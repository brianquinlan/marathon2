"""
Task model and priority ranking module.
Associates tasks with user issues in Firestore, tracks priority update requirements,
and dispatches asynchronous ranking tasks using Firebase Task Queue Functions (firebase_admin.functions.task_queue).
See: https://firebase.google.com/docs/functions/task-functions#python
"""

import concurrent.futures
import logging
from datetime import datetime

from firebase_admin import functions as admin_functions
from google.cloud import firestore
from pydantic import BaseModel, ConfigDict, field_serializer

from genai_ranker import run_ranker

logger = logging.getLogger(__name__)

_ranking_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="ranking-worker")


class Task(BaseModel):
    """
    Represents a task associated with an authenticated user and a specific GitHub issue.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    priority: float = 0.0  # A priority value between 0.0 and 1.0
    priority_needs_updated: bool = True
    github_issue_id: str | None = None  # Direct string key (e.g. owner_repo_number)
    owner: str | None = None
    repo: str | None = None
    issue_number: int | None = None
    uid: str | None = None  # Owner user ID
    id: str | None = None  # Task document ID (e.g. task_{github_issue_id})
    github_issue_title: str | None = None  # Optional cached title copied from GitHub issue
    github_issue_url: str | None = None  # Optional direct URL copied from GitHub issue
    github_issue_upvotes: int = 0  # Number of +1 upvotes / reactions on the GitHub issue
    association_reasons: list[str] = []
    created_at: datetime | str | object | None = None
    updated_at: datetime | str | object | None = None

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_timestamps(self, v: datetime | str | object | None) -> str | None:
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


# ============================================================================
# Asynchronous Task Enqueuing via Firebase Admin SDK
# (https://firebase.google.com/docs/functions/task-functions#python)
# ============================================================================


def enqueue_task_ranking(
    uid: str,
    task_id: str,
    function_name: str = "rank_user_tasks",
    db: firestore.Client | None = None,
    opts: admin_functions.TaskOptions | None = None,
) -> dict[str, object]:
    """
    Enqueues a task to the Firebase Task Queue function using the official Firebase Admin SDK.
    Dispatches a payload with the user UID and task document ID.
    See: https://firebase.google.com/docs/functions/task-functions#python
    """
    try:
        queue = admin_functions.task_queue(function_name)
        task_opts = opts or admin_functions.TaskOptions(dispatch_deadline_seconds=300)
        enqueued_id = queue.enqueue({"uid": uid, "task_id": task_id}, opts=task_opts)
        logger.info(
            f"Enqueued Firebase task '{enqueued_id}' in queue '{function_name}' for task '{task_id}' (UID {uid})"
        )
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
        # dispatch asynchronously via bounded thread pool if db client is provided
        if db is not None:

            def async_worker():
                try:
                    update_task_priority(uid=uid, task_id=task_id, db=db)
                except Exception as ex:
                    logger.error(f"Error in async ranking worker for task {task_id} (UID {uid}): {ex}")

            _ranking_executor.submit(async_worker)

        return {
            "status": "enqueued",
            "mode": "async_dispatched_fallback",
            "target_task_id": task_id,
            "uid": uid,
        }


# ============================================================================
# Task Management & Firestore Operations
# ============================================================================


def ensure_task_for_issue(uid: str, issue_id: str, issue_data: dict[str, object], db: firestore.Client) -> Task:
    """
    Creates or updates the Task associated with a given issue in Firestore under users/{uid}/tasks.
    When an issue is modified or created, priority_needs_updated is set to True.
    """
    task_doc_id = f"task_{issue_id}"
    tasks_col = db.collection("users").document(uid).collection("tasks")
    task_ref = tasks_col.document(task_doc_id)
    doc_snap = task_ref.get()

    raw_title = issue_data.get("title")
    issue_title = str(raw_title) if raw_title is not None else None
    raw_url = issue_data.get("url")
    issue_url = str(raw_url) if raw_url is not None else None
    raw_upvotes = issue_data.get("upvotes")
    issue_upvotes = int(raw_upvotes) if isinstance(raw_upvotes, int) else 0

    owner = str(issue_data.get("owner")) if issue_data.get("owner") is not None else None
    repo = str(issue_data.get("repo")) if issue_data.get("repo") is not None else None
    raw_num = issue_data.get("issue_number") or issue_data.get("number")
    issue_number = int(raw_num) if isinstance(raw_num, (int, str)) and str(raw_num).isdigit() else None

    raw_reasons = issue_data.get("association_reasons", [])
    reasons_list: list[str] = [str(r) for r in raw_reasons] if isinstance(raw_reasons, list) else []

    if doc_snap.exists:
        raw_dict = doc_snap.to_dict() or {}
        task = Task.model_validate({**raw_dict, "id": task_doc_id})
        task.priority_needs_updated = True
        task.github_issue_title = issue_title or task.github_issue_title
        task.github_issue_url = issue_url or task.github_issue_url
        task.github_issue_upvotes = issue_upvotes if issue_upvotes > 0 else task.github_issue_upvotes
        task.github_issue_id = issue_id
        task.owner = owner or task.owner
        task.repo = repo or task.repo
        task.issue_number = issue_number or task.issue_number
        if reasons_list:
            combined = set(task.association_reasons + reasons_list)
            task.association_reasons = sorted(list(combined))
    else:
        task = Task(
            id=task_doc_id,
            priority=0.0,
            priority_needs_updated=True,
            github_issue_id=issue_id,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            uid=uid,
            github_issue_title=issue_title,
            github_issue_url=issue_url,
            github_issue_upvotes=issue_upvotes,
            association_reasons=reasons_list,
        )

    task_data = task.model_dump()
    task_data["updated_at"] = firestore.SERVER_TIMESTAMP
    if task.created_at is None:
        task_data["created_at"] = firestore.SERVER_TIMESTAMP
    task_ref.set(task_data, merge=True)
    return task


def update_task_priority(uid: str, task_id: str, db: firestore.Client) -> dict[str, object]:
    """
    Retrieves a single task from Firestore for a given user, loads its associated
    GitHub issue and comments in-memory using the user's github_access_token, calls the
    ranker, and persists the updated priority back to Firestore.
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

    # Fetch user profile to get github_access_token, github_username and gemini_api_key
    user_ref = db.collection("users").document(uid)
    user_snap = user_ref.get()
    github_access_token: str | None = None
    github_username: str | None = None
    gemini_api_key: str | None = None
    if user_snap.exists:
        u_dict = user_snap.to_dict() or {}
        raw_tok = u_dict.get("github_access_token")
        github_access_token = str(raw_tok) if raw_tok is not None else None
        raw_u_name = u_dict.get("github_username")
        github_username = str(raw_u_name) if raw_u_name is not None else None
        raw_key = u_dict.get("gemini_api_key")
        gemini_api_key = str(raw_key) if raw_key is not None else None
        logger.info(
            f"[UPDATE_TASK_PRIORITY] User profile loaded: github_username='{github_username}', "
            f"has_token={bool(github_access_token)}, has_gemini_key={bool(gemini_api_key)}"
        )
    else:
        logger.warning(f"[UPDATE_TASK_PRIORITY] User profile document users/{uid} does not exist.")

    # Fetch issue details and comments in-memory via PyGithub
    from github_sync import IssuePayload, fetch_issue_in_memory

    issue_payload: IssuePayload | dict[str, object] | None = None
    owner = task.owner
    repo = task.repo
    num = task.issue_number

    # Fallback parsing from github_issue_id or github_issue_url if not explicitly set on Task
    if (not owner or not repo or not num) and task.github_issue_id:
        parts = task.github_issue_id.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            num = int(parts[1])
            repo_parts = parts[0].split("_", 1)
            if len(repo_parts) == 2:
                owner, repo = repo_parts[0], repo_parts[1]

    if github_access_token and owner and repo and num:
        try:
            issue_payload = fetch_issue_in_memory(
                access_token=github_access_token,
                owner=owner,
                repo=repo,
                issue_number=num,
            )
            comments_len = len(issue_payload.comments)
            logger.info(
                f"[UPDATE_TASK_PRIORITY] Fetched in-memory issue {owner}/{repo}#{num} with {comments_len} comments."
            )
        except Exception as e:
            logger.warning(f"[UPDATE_TASK_PRIORITY] Error fetching in-memory issue data for {owner}/{repo}#{num}: {e}")

    # If in-memory fetch wasn't available, provide basic fallback dict from task cached fields
    if not issue_payload:
        issue_payload = {
            "title": task.github_issue_title,
            "url": task.github_issue_url,
            "upvotes": task.github_issue_upvotes,
            "comments": [],
        }

    ranked_task = run_ranker(
        task=task, issue=issue_payload, github_username=github_username, gemini_api_key=gemini_api_key
    )

    update_payload = {
        "priority": ranked_task.priority,
        "priority_needs_updated": False,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    logger.info(f"[UPDATE_TASK_PRIORITY] Writing updated task {task_id} to Firestore with payload: {update_payload}")
    task_ref.set(update_payload, merge=True)

    logger.info(
        f"[UPDATE_TASK_PRIORITY] Successfully updated priority for task {task_id} for user {uid} -> {ranked_task.priority:.2f}."
    )
    return {
        "status": "success",
        "task_id": task_id,
        "uid": uid,
        "priority": ranked_task.priority,
        "task": ranked_task.model_dump(mode="json"),
    }


def force_rerank_tasks(uid: str, db: firestore.Client) -> dict[str, object]:
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
        batch.set(
            doc_snap.reference,
            {
                "priority_needs_updated": True,
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        count += 1

    if count > 0:
        batch.commit()
        logger.info(f"Marked {count} tasks as priority_needs_updated=True for forced rerank.")

    return {
        "status": "marked",
        "marked_count": count,
        "message": f"Marked {count} tasks for rerank.",
    }


def get_user_tasks(uid: str, db: firestore.Client, limit: int = 100) -> list[dict[str, object]]:
    """
    Retrieves tasks stored in Firestore for a given user UID, sorted by priority (descending).
    """
    tasks_col = db.collection("users").document(uid).collection("tasks")
    docs = tasks_col.limit(limit).stream()

    tasks: list[dict[str, object]] = []
    for doc_snap in docs:
        raw_dict = doc_snap.to_dict() or {}
        t = Task.model_validate({**raw_dict, "id": doc_snap.id})
        tasks.append(t.model_dump(mode="json"))

    # Sort descending by priority
    tasks.sort(key=lambda x: float(x.get("priority", 0.0)), reverse=True)  # type: ignore
    return tasks
