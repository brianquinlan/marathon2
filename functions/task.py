"""
Task model and priority ranking module.
Associates tasks with user issues in Firestore, tracks priority update requirements,
and dispatches asynchronous ranking tasks using Firebase Task Queue Functions (firebase_admin.functions.task_queue).
See: https://firebase.google.com/docs/functions/task-functions#python
"""

from datetime import datetime, timezone
import json
import os
import threading
from typing import Any, Dict, List, Optional
import logging
from pydantic import BaseModel, Field, ConfigDict, field_serializer
from google.cloud import firestore
import firebase_admin
from firebase_admin import functions as admin_functions

logger = logging.getLogger(__name__)


class Task(BaseModel):
    """
    Represents a task associated with an authenticated user and a specific GitHub issue.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    priority: float = 0.0  # A priority value between 0.0 and 1.0
    priority_needs_updated: bool = True
    github_issue_ref: Optional[Any] = None  # Native Firestore DocumentReference or resource path
    github_issue_id: Optional[str] = None  # Direct string key (e.g. owner_repo_number)
    uid: Optional[str] = None  # Owner user ID
    id: Optional[str] = None  # Task document ID (e.g. task_{github_issue_id})
    github_issue_title: Optional[str] = None  # Optional cached title copied from GitHub issue
    github_issue_url: Optional[str] = None  # Optional direct URL copied from GitHub issue
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None

    @field_serializer("github_issue_ref", when_used="json")
    def serialize_issue_ref(self, v: Any) -> Optional[str]:
        if v is None:
            return None
        if hasattr(v, "path"):
            return str(v.path)
        return str(v)

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_timestamps(self, v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)

    @property
    def doc_id(self) -> str:
        """Standardized document ID for Firestore."""
        if self.id:
            return self.id
        if self.github_issue_id:
            return f"task_{self.github_issue_id}"
        return "task_unknown"


from genai_ranker import (
    TaskPriorityOutput,
    get_pydantic_ai_agent,
    run_ranker,
)


# ============================================================================
# Asynchronous Task Enqueuing via Firebase Admin SDK
# (https://firebase.google.com/docs/functions/task-functions#python)
# ============================================================================

def enqueue_task_ranking(
    uid: str,
    task_id: str,
    function_name: str = "rank_user_tasks",
    db: Optional[firestore.Client] = None,
    opts: Optional[admin_functions.TaskOptions] = None
) -> Dict[str, Any]:
    """
    Enqueues a task to the Firebase Task Queue function using the official Firebase Admin SDK.
    Dispatches a payload with the user UID and task document ID.
    See: https://firebase.google.com/docs/functions/task-functions#python
    """
    try:
        queue = admin_functions.task_queue(function_name)
        task_opts = opts or admin_functions.TaskOptions(dispatch_deadline_seconds=300)
        enqueued_id = queue.enqueue({"uid": uid, "task_id": task_id}, opts=task_opts)
        logger.info(f"Enqueued Firebase task '{enqueued_id}' in queue '{function_name}' for task '{task_id}' (UID {uid})")
        return {
            "status": "enqueued",
            "task_id": enqueued_id,
            "target_task_id": task_id,
            "queue": function_name,
            "uid": uid,
        }
    except Exception as e:
        logger.warning(f"Firebase task_queue.enqueue exception ({e}). Handling fallback dispatch.")
        # In local emulator or unauthenticated testing environments without Cloud Tasks credentials,
        # dispatch asynchronously via thread if db client is provided
        if db is not None:
            def async_worker():
                try:
                    update_task_priority(uid=uid, task_id=task_id, db=db)
                except Exception as ex:
                    logger.error(f"Error in async ranking worker for task {task_id} (UID {uid}): {ex}")

            thread = threading.Thread(target=async_worker, daemon=True)
            thread.start()

        return {
            "status": "enqueued",
            "mode": "async_dispatched_fallback",
            "target_task_id": task_id,
            "uid": uid,
        }


# ============================================================================
# Task Management & Firestore Operations
# ============================================================================

def ensure_task_for_issue(
    uid: str,
    issue_id: str,
    issue_data: Dict[str, Any],
    db: firestore.Client
) -> Task:
    """
    Creates or updates the Task associated with a given issue in Firestore.
    When an issue is modified or created, priority_needs_updated is set to True.
    """
    task_doc_id = f"task_{issue_id}"
    tasks_col = db.collection("users").document(uid).collection("tasks")
    task_ref = tasks_col.document(task_doc_id)
    doc_snap = task_ref.get()

    issue_ref = db.collection("users").document(uid).collection("issues").document(issue_id)
    issue_title = issue_data.get("title")
    issue_url = issue_data.get("url")

    if doc_snap.exists:
        task = Task.model_validate({**(doc_snap.to_dict() or {}), "id": task_doc_id})
        task.priority_needs_updated = True
        task.github_issue_title = issue_title or task.github_issue_title
        task.github_issue_url = issue_url or task.github_issue_url
        task.github_issue_ref = issue_ref
        task.github_issue_id = issue_id
    else:
        task = Task(
            id=task_doc_id,
            priority=0.0,
            priority_needs_updated=True,
            github_issue_ref=issue_ref,
            github_issue_id=issue_id,
            uid=uid,
            github_issue_title=issue_title,
            github_issue_url=issue_url,
        )

    task_data = task.model_dump()
    task_data["updated_at"] = firestore.SERVER_TIMESTAMP
    if task.created_at is None:
        task_data["created_at"] = firestore.SERVER_TIMESTAMP
    task_ref.set(task_data, merge=True)
    return task


def update_task_priority(uid: str, task_id: str, db: firestore.Client) -> Dict[str, Any]:
    """
    Retrieves a single task from Firestore for a given user, loads its associated
    GitHub issue (including comments) and the user's github_username, calls the
    Genkit ranker, and persists the updated priority back to Firestore.
    """
    logger.info(f"[UPDATE_TASK_PRIORITY] Starting update_task_priority: uid={uid}, task_id={task_id}")
    task_ref = db.collection("users").document(uid).collection("tasks").document(task_id)
    doc_snap = task_ref.get()
    if not doc_snap.exists:
        logger.warning(f"[UPDATE_TASK_PRIORITY] Task document {task_id} NOT found for UID {uid}.")
        return {"status": "not_found", "task_id": task_id, "uid": uid}

    raw_task_data = doc_snap.to_dict() or {}
    task = Task.model_validate({**raw_task_data, "id": task_id})
    logger.info(
        f"[UPDATE_TASK_PRIORITY] Loaded task {task_id}: title='{task.github_issue_title}', "
        f"priority={task.priority}, priority_needs_updated={task.priority_needs_updated}, "
        f"github_issue_id='{task.github_issue_id}'"
    )

    # Fetch associated issue and comments from users/{uid}/issues/{issue_id}
    issue_id = task.github_issue_id or (task_id[5:] if task_id.startswith("task_") else task_id)
    issue_data = {}
    if issue_id:
        issue_ref = db.collection("users").document(uid).collection("issues").document(issue_id)
        issue_snap = issue_ref.get()
        if issue_snap.exists:
            issue_data = issue_snap.to_dict() or {}
            logger.info(f"[UPDATE_TASK_PRIORITY] Found issue doc {issue_id} with {len(issue_data.get('comments', []))} comments.")
        else:
            logger.warning(f"[UPDATE_TASK_PRIORITY] Issue doc {issue_id} not found in users/{uid}/issues/.")

    # Fetch user profile to get github_username and gemini_api_key
    user_ref = db.collection("users").document(uid)
    user_snap = user_ref.get()
    github_username = None
    gemini_api_key = None
    if user_snap.exists:
        u_dict = user_snap.to_dict() or {}
        github_username = u_dict.get("github_username")
        gemini_api_key = u_dict.get("gemini_api_key")
        logger.info(
            f"[UPDATE_TASK_PRIORITY] User profile loaded: github_username='{github_username}', "
            f"has_gemini_key={bool(gemini_api_key)}"
        )
    else:
        logger.warning(f"[UPDATE_TASK_PRIORITY] User profile document users/{uid} does not exist.")

    ranked_task = run_ranker(
        task=task,
        issue=issue_data,
        github_username=github_username,
        gemini_api_key=gemini_api_key
    )

    update_payload = {
        "priority": ranked_task.priority,
        "priority_needs_updated": False,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    logger.info(f"[UPDATE_TASK_PRIORITY] Writing updated task {task_id} to Firestore with payload: {update_payload}")
    task_ref.set(update_payload, merge=True)

    logger.info(f"[UPDATE_TASK_PRIORITY] Successfully updated priority for task {task_id} for user {uid} -> {ranked_task.priority:.2f}.")
    return {
        "status": "success",
        "task_id": task_id,
        "uid": uid,
        "priority": ranked_task.priority,
        "task": ranked_task.model_dump(mode="json"),
    }


def force_rerank_tasks(uid: str, db: firestore.Client) -> Dict[str, Any]:
    """
    Forces all tasks for the user to be reranked asynchronously:
    Sets priority_needs_updated = True for all tasks in Firestore.
    The on_task_written trigger will automatically detect each task write and enqueue ranking.
    """
    tasks_col = db.collection("users").document(uid).collection("tasks")
    all_docs = tasks_col.stream()

    batch = db.batch()
    count = 0

    for doc_snap in all_docs:
        batch.set(doc_snap.reference, {
            "priority_needs_updated": True,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        count += 1

    if count > 0:
        batch.commit()
        logger.info(f"Marked {count} tasks as priority_needs_updated=True for forced rerank.")

    return {
        "status": "marked",
        "marked_count": count,
        "message": f"Marked {count} tasks for rerank.",
    }


def get_user_tasks(uid: str, db: firestore.Client, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Retrieves tasks stored in Firestore for a given user UID, sorted by priority (descending).
    """
    tasks_col = db.collection("users").document(uid).collection("tasks")
    docs = tasks_col.limit(limit).stream()

    tasks: List[Dict[str, Any]] = []
    for doc_snap in docs:
        t = Task.model_validate({**(doc_snap.to_dict() or {}), "id": doc_snap.id})
        tasks.append(t.model_dump(mode="json"))

    # Sort descending by priority
    tasks.sort(key=lambda x: x.get("priority", 0.0), reverse=True)
    return tasks
