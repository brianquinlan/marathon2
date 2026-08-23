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
from pydantic import BaseModel, Field, ConfigDict
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

    @property
    def doc_id(self) -> str:
        """Standardized document ID for Firestore."""
        if self.id:
            return self.id
        if self.github_issue_id:
            return f"task_{self.github_issue_id}"
        return "task_unknown"


# ============================================================================
# Ranker Engine
# ============================================================================

def run_ranker(tasks: List[Task]) -> List[Task]:
    """
    Ranker engine that computes priorities for tasks needing updates.
    Currently a placeholder that resets priority_needs_updated to False.
    """
    logger.info(f"Ranker engine processing {len(tasks)} tasks.")
    for task in tasks:
        task.priority_needs_updated = False
    return tasks


# ============================================================================
# Asynchronous Task Enqueuing via Firebase Admin SDK
# (https://firebase.google.com/docs/functions/task-functions#python)
# ============================================================================

def enqueue_task_ranking(
    uid: str,
    function_name: str = "rank_user_tasks",
    db: Optional[firestore.Client] = None,
    opts: Optional[admin_functions.TaskOptions] = None
) -> Dict[str, Any]:
    """
    Enqueues a task to the Firebase Task Queue function using the official Firebase Admin SDK.
    See: https://firebase.google.com/docs/functions/task-functions#python
    """
    try:
        queue = admin_functions.task_queue(function_name)
        task_opts = opts or admin_functions.TaskOptions(dispatch_deadline_seconds=300)
        task_id = queue.enqueue({"uid": uid}, opts=task_opts)
        logger.info(f"Enqueued Firebase task '{task_id}' in queue '{function_name}' for UID {uid}")
        return {
            "status": "enqueued",
            "task_id": task_id,
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
                    update_needed_priorities(uid=uid, db=db)
                except Exception as ex:
                    logger.error(f"Error in async ranking worker for UID {uid}: {ex}")

            thread = threading.Thread(target=async_worker, daemon=True)
            thread.start()

        return {
            "status": "enqueued",
            "mode": "async_dispatched_fallback",
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


def update_needed_priorities(uid: str, db: firestore.Client) -> Dict[str, Any]:
    """
    Finds all tasks for the user where priority_needs_updated == True,
    calls the ranker, and persists the updated priorities back to Firestore.
    """
    tasks_col = db.collection("users").document(uid).collection("tasks")
    docs = tasks_col.where("priority_needs_updated", "==", True).stream()

    tasks_to_update: List[Task] = []
    doc_refs: Dict[str, Any] = {}

    for doc_snap in docs:
        t = Task.model_validate({**(doc_snap.to_dict() or {}), "id": doc_snap.id})
        tasks_to_update.append(t)
        doc_refs[t.doc_id] = doc_snap.reference

    if not tasks_to_update:
        logger.info(f"No tasks require priority update for user {uid}.")
        return {
            "status": "success",
            "updated_count": 0,
            "message": "All tasks are already up-to-date.",
            "tasks": []
        }

    # Execute ranker
    ranked_tasks = run_ranker(tasks_to_update)

    # Batch persist updated priorities
    batch = db.batch()
    for task in ranked_tasks:
        ref = doc_refs.get(task.doc_id) or tasks_col.document(task.doc_id)
        batch.set(ref, {
            "priority": task.priority,
            "priority_needs_updated": False,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)
    batch.commit()

    logger.info(f"Updated priorities for {len(ranked_tasks)} tasks for user {uid}.")
    return {
        "status": "success",
        "updated_count": len(ranked_tasks),
        "message": f"Updated priorities for {len(ranked_tasks)} tasks.",
        "tasks": [t.model_dump(mode="json") for t in ranked_tasks],
    }


def force_rerank_tasks(uid: str, db: firestore.Client) -> Dict[str, Any]:
    """
    Forces all tasks for the user to be reranked asynchronously:
    1. Sets priority_needs_updated = True for all tasks in Firestore.
    2. Enqueues a task to run the ranking in the background using Firebase task_queue.
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

    # Enqueue via Firebase Admin Task Queue
    enqueue_result = enqueue_task_ranking(uid=uid, function_name="rank_user_tasks", db=db)
    return {
        "status": "enqueued",
        "marked_count": count,
        "message": f"Marked {count} tasks for rerank and enqueued ranking task.",
        "enqueue_info": enqueue_result,
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
