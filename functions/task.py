"""
Task model and priority ranking module.
Associates tasks with user issues in Firestore, tracks priority update requirements,
and dispatches asynchronous ranking tasks using Firebase Task Queue Functions (firebase_admin.functions.task_queue).
See: https://firebase.google.com/docs/functions/task-functions#python
"""

from datetime import datetime

from firebase_admin import functions as admin_functions
from google.cloud import firestore
from pydantic import BaseModel, ConfigDict
from queue_utils import dispatch_task

from genai_ranker import run_ranker


class Task(BaseModel):
    """
    Represents a task associated with an authenticated user and a specific GitHub issue.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    priority: float = 0.0  # A priority value between 0.0 and 1.0
    priority_needs_updated: bool = True
    owner: str | None = None
    repo: str | None = None
    issue_number: int | None = None
    github_issue_title: str | None = None  # Optional cached title copied from GitHub issue
    github_issue_url: str | None = None  # Optional direct URL copied from GitHub issue
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def doc_id(self) -> str:
        """Standardized document ID for Firestore."""
        if self.owner and self.repo and self.issue_number:
            return f"task_{self.owner}_{self.repo}_{self.issue_number}"
        return "task_unknown"


# ============================================================================
# Asynchronous Task Enqueuing via Task Queue Abstraction
# ============================================================================


def enqueue_task_ranking(
    uid: str,
    task_id: str,
    db: firestore.Client,
    function_name: str = "rank_user_tasks",
    opts: admin_functions.TaskOptions | None = None,
) -> None:
    """
    Enqueues a task to the Firebase Task Queue function using the task queue abstraction.
    Dispatches a payload with the user UID and task document ID.
    """
    dispatch_task(
        queue_name=function_name,
        task_data={"uid": uid, "task_id": task_id},
        worker_fn=lambda: update_task_priority(uid=uid, task_id=task_id, db=db),
        opts=opts,
    )


# ============================================================================
# Task Management & Firestore Operations
# ============================================================================


def ensure_task_for_issue(uid: str, issue_id: str, issue_data: dict[str, object], db: firestore.Client) -> None:
    """
    Creates or updates the Task associated with a given issue in Firestore under users/{uid}/tasks.
    When an issue is modified or created, priority_needs_updated is set to True.
    """
    task_doc_id = f"task_{issue_id}"
    tasks_col = db.collection("users").document(uid).collection("tasks")
    task_ref = tasks_col.document(task_doc_id)
    doc_snap = task_ref.get()

    raw_title = issue_data.get("title") or issue_data.get("github_issue_title")
    issue_title = str(raw_title) if raw_title is not None else None
    raw_url = issue_data.get("url") or issue_data.get("github_issue_url")
    issue_url = str(raw_url) if raw_url is not None else None

    owner = str(issue_data.get("owner")) if issue_data.get("owner") is not None else None
    repo = str(issue_data.get("repo")) if issue_data.get("repo") is not None else None
    raw_num = issue_data.get("issue_number") or issue_data.get("number")
    issue_number = int(raw_num) if isinstance(raw_num, (int, str)) and str(raw_num).isdigit() else None

    if doc_snap.exists:
        raw_dict = doc_snap.to_dict()
        if not isinstance(raw_dict, dict):
            raw_dict = {}
        task = Task.model_validate(raw_dict)
        task.priority_needs_updated = True
        task.github_issue_title = issue_title or task.github_issue_title
        task.github_issue_url = issue_url or task.github_issue_url
        task.owner = owner or task.owner
        task.repo = repo or task.repo
        task.issue_number = issue_number or task.issue_number
    else:
        task = Task(
            priority=0.0,
            priority_needs_updated=True,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            github_issue_title=issue_title,
            github_issue_url=issue_url,
        )

    task_data = task.model_dump()
    task_data["updated_at"] = firestore.SERVER_TIMESTAMP
    if task.created_at is None:
        task_data["created_at"] = firestore.SERVER_TIMESTAMP
    task_ref.set(task_data, merge=True)


def update_task_priority(uid: str, task_id: str, db: firestore.Client) -> None:
    """
    Retrieves a single task from Firestore for a given user, loads its associated
    GitHub issue and comments in-memory using the user's github_access_token, calls the
    ranker, and persists the updated priority back to Firestore.
    """
    task_ref = db.collection("users").document(uid).collection("tasks").document(task_id)
    doc_snap = task_ref.get()
    if not doc_snap.exists:
        return

    raw_task_data = doc_snap.to_dict()
    if not isinstance(raw_task_data, dict):
        raw_task_data = {}
    task = Task.model_validate(raw_task_data)

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

    # Fetch issue details and comments in-memory via PyGithub
    from genai_ranker import IssuePayload
    from github_sync import fetch_issue_in_memory

    issue_payload: IssuePayload | dict[str, object] | None = None
    owner = task.owner
    repo = task.repo
    num = task.issue_number

    # Fallback parsing from task_id if not explicitly set on Task
    if (not owner or not repo or not num) and task_id:
        clean_id = task_id.removeprefix("task_")
        parts = clean_id.rsplit("_", 1)
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
        except Exception:
            pass

    # If in-memory fetch wasn't available, provide basic fallback dict from task cached fields
    if not issue_payload:
        issue_payload = {
            "title": task.github_issue_title,
            "url": task.github_issue_url,
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
    task_ref.set(update_payload, merge=True)


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
        t = Task.model_validate(raw_dict)
        t_dict = t.model_dump(mode="json")
        t_dict["id"] = doc_snap.id
        tasks.append(t_dict)

    # Sort descending by priority
    tasks.sort(key=lambda x: float(x.get("priority", 0.0)), reverse=True)  # type: ignore
    return tasks
