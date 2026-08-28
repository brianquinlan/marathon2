"""
Firebase Functions (2nd Gen) Python Backend
Associates and manages user-specific information for users authenticated via
Firebase Authentication (Google and GitHub providers supported).
Supports:
1. Asynchronous chained GitHub issue & comment pagination via Task Queue Functions
2. Task prioritization and asynchronous ranking lifecycle via Task Queue Functions
"""

import firebase_admin
from firebase_functions import firestore_fn, https_fn, options, scheduler_fn, tasks_fn
from google.cloud import firestore

from dev import render_main_page, render_settings_page
from github_sync import (
    enqueue_issue_page_sync,
    enqueue_user_periodic_sync,
    start_user_github_sync,
    sync_user_periodic,
)
from task import (
    cleanup_repo_tasks,
    delete_all_user_tasks,
    enqueue_task_ranking,
    force_rerank_tasks,
    get_user_tasks,
    mark_all_tasks_for_reranking,
    update_task_priority,
)
from user import User

# Initialize Firebase Admin App if not already initialized
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db: firestore.Client = firestore.Client()

__all__ = [
    "render_main_page",
    "render_settings_page",
]


@https_fn.on_call(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def force_rerank_all_tasks(req: https_fn.CallableRequest) -> dict[str, object]:
    """
    Forces all tasks for the authenticated user to be reranked:
    Sets priority_needs_updated = True on all tasks and enqueues the ranker.
    """
    if not req.auth or not req.auth.uid:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to force rerank tasks.",
        )

    uid = str(req.auth.uid)
    try:
        result = force_rerank_tasks(uid=uid, db=db)
        return result
    except Exception as e:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL, message=f"Failed to force rerank tasks: {e!s}"
        ) from e


@https_fn.on_call(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def get_user_task_list(req: https_fn.CallableRequest) -> dict[str, object]:
    """
    Retrieves all tasks for the authenticated user from Firestore under users/{uid}/tasks.
    """
    if not req.auth or not req.auth.uid:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED, message="User must be authenticated to retrieve tasks."
        )

    uid = str(req.auth.uid)
    payload: dict[str, object] = req.data if isinstance(req.data, dict) else {}
    raw_lim = payload.get("limit", 100)
    limit = int(raw_lim) if isinstance(raw_lim, (int, str)) and str(raw_lim).isdigit() else 100

    tasks = get_user_tasks(uid=uid, db=db, limit=limit)
    return {
        "status": "success",
        "count": len(tasks),
        "tasks": tasks,
    }


# ============================================================================
# Firebase Task Queue Functions: Issue Pagination
# ============================================================================


@tasks_fn.on_task_dispatched(
    retry_config=options.RetryConfig(max_attempts=3, min_backoff_seconds=10, max_backoff_seconds=300, max_doublings=3),
    rate_limits=options.RateLimits(max_concurrent_dispatches=10, max_dispatches_per_second=10),
)
def sync_github_issues_page(req: tasks_fn.CallableRequest) -> None:
    """
    Processes one page of GitHub issues for a given user, updates Tasks in Firestore,
    and chains to the next page if available.
    """
    data: dict[str, object] = req.data if isinstance(req.data, dict) else {}
    raw_uid = data.get("uid")
    uid = str(raw_uid) if raw_uid is not None else None
    raw_filter = data.get("filter_name")
    filter_name = str(raw_filter) if raw_filter is not None else None
    raw_repo_full = data.get("repo_full_name")
    repo_full_name = str(raw_repo_full) if raw_repo_full is not None else None
    state = str(data.get("state", "open"))
    raw_since = data.get("since")
    since = str(raw_since) if raw_since is not None else None
    raw_page = data.get("page", 0)
    page = int(raw_page) if isinstance(raw_page, (int, str)) and str(raw_page).isdigit() else 0
    raw_per_page = data.get("per_page", 100)
    per_page = int(raw_per_page) if isinstance(raw_per_page, (int, str)) and str(raw_per_page).isdigit() else 100
    raw_owner = data.get("owner_fallback")
    owner_fallback = str(raw_owner) if raw_owner is not None else None
    raw_repo = data.get("repo_fallback")
    repo_fallback = str(raw_repo) if raw_repo is not None else None

    if not uid:
        raise tasks_fn.HttpsError(
            code=tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT, message="Missing 'uid' in task data."
        )

    from github_sync import execute_issue_page_sync

    execute_issue_page_sync(
        uid=uid,
        db=db,
        filter_name=filter_name,
        repo_full_name=repo_full_name,
        state=state,
        since=since,
        page=page,
        per_page=per_page,
        owner_fallback=owner_fallback,
        repo_fallback=repo_fallback,
    )


# ============================================================================
# Firebase Task Queue Functions: Task Ranking
# ============================================================================


@tasks_fn.on_task_dispatched(
    retry_config=options.RetryConfig(max_attempts=3, min_backoff_seconds=10, max_backoff_seconds=300, max_doublings=3),
    rate_limits=options.RateLimits(max_concurrent_dispatches=5, max_dispatches_per_second=10),
)
def rank_user_tasks(req: tasks_fn.CallableRequest) -> None:
    """
    Firebase Task Queue function for asynchronous ranking of a single task.
    Dispatched via Cloud Tasks when a task's priority needs update.
    """
    data: dict[str, object] = req.data if isinstance(req.data, dict) else {}
    raw_uid = data.get("uid")
    uid = str(raw_uid) if raw_uid is not None else None
    raw_tid = data.get("task_id")
    task_id = str(raw_tid) if raw_tid is not None else None
    if not uid or not task_id:
        raise tasks_fn.HttpsError(
            code=tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT, message="Missing 'uid' or 'task_id' in task payload."
        )

    update_task_priority(uid=uid, task_id=task_id, db=db)


# ============================================================================
# Scheduled Functions & Task Queue Workers: 20-Minute Periodic User Sync
# ============================================================================


@tasks_fn.on_task_dispatched()
def sync_user_periodic_task(req: tasks_fn.CallableRequest) -> None:
    """
    Task Queue worker that runs the 20-minute periodic sync for a single user,
    purging closed tasks and syncing updated/new open issues.
    """
    payload = req.data if isinstance(req.data, dict) else {}
    uid = payload.get("uid")
    if not uid:
        raise tasks_fn.HttpsError(
            code=tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="Periodic user sync task requires 'uid'.",
        )
    sync_user_periodic(uid=str(uid), db=db)


@scheduler_fn.on_schedule(schedule="every 20 minutes", retry_count=1)
def periodic_github_sync_scheduler(event: scheduler_fn.ScheduledEvent) -> None:
    """
    Scheduled Cloud Function that runs every 20 minutes.
    Sweeps all users in Firestore and enqueues individual user periodic sync jobs
    for users with an active GitHub access token.
    """
    users_col = db.collection("users")
    users_docs = users_col.stream()

    for doc_snap in users_docs:
        raw_user_dict = doc_snap.to_dict() or {}
        if raw_user_dict.get("github_access_token"):
            enqueue_user_periodic_sync(uid=doc_snap.id, db=db)


# ============================================================================
# Cloud Firestore Triggers: Auto-Sync on User Settings Creation or Change
# ============================================================================


@firestore_fn.on_document_written(document="users/{uid}")
def on_user_settings_changed(
    event: firestore_fn.Event[firestore_fn.Change[firestore_fn.DocumentSnapshot | None]],
) -> None:
    """
    Cloud Firestore trigger that handles targeted lifecycle actions when user settings change:
    1. If GitHub access token changes (or is newly added): discard all tasks and perform a full sync.
    2. If Gemini API key changes: mark every task as needing re-ranked and enqueue ranking.
    3. If new repo(s) added: enqueue sync task for only the newly added repo(s).
    4. If repo(s) removed: cleanup tasks associated with removed repo(s) (preserving tasks with other sources).
    """
    if event.data is None or event.data.after is None:
        return

    after_snap = event.data.after
    before_snap = event.data.before

    # If document does not exist after write, do nothing
    if not after_snap.exists:
        return

    uid_param = event.params.get("uid")
    if not uid_param:
        return
    uid = str(uid_param)

    after_data = after_snap.to_dict() or {}
    before_data = {}
    is_new = (before_snap is None) or not before_snap.exists
    if not is_new and before_snap is not None:
        before_data = before_snap.to_dict() or {}

    after_token = after_data.get("github_access_token")
    before_token = before_data.get("github_access_token")
    after_gemini = after_data.get("gemini_api_key")
    before_gemini = before_data.get("gemini_api_key")

    after_repos = set((after_data.get("monitored_repos") or {}).keys())
    before_repos = set((before_data.get("monitored_repos") or {}).keys())

    # Case 1: GitHub Access Token changed / added / removed
    if is_new or (after_token != before_token):
        if after_token:
            delete_all_user_tasks(uid=uid, db=db)
            user = User.model_validate({**after_data, "uid": uid})
            # Reset sync timestamps to trigger full sync
            user.last_assigned_sync = None
            user.last_mentioned_sync = None
            user.last_created_sync = None
            user.monitored_repos = {r: None for r in after_repos}
            start_user_github_sync(user=user, db=db, state="open")
            return
        else:
            # Token removed
            delete_all_user_tasks(uid=uid, db=db)
            return

    # Case 2: Gemini API Key changed
    if after_gemini != before_gemini and after_gemini:
        mark_all_tasks_for_reranking(uid=uid, db=db)

    # Case 3: Monitored repos added
    added_repos = after_repos - before_repos
    for repo_clean in added_repos:
        repo_clean = repo_clean.strip().strip("/")
        if not repo_clean or "/" not in repo_clean:
            continue
        owner_part, repo_part = repo_clean.split("/", 1)
        enqueue_issue_page_sync(
            uid=uid,
            db=db,
            repo_full_name=repo_clean,
            state="open",
            since=None,
            page=0,
            per_page=100,
            owner_fallback=owner_part,
            repo_fallback=repo_part,
        )

    # Case 4: Monitored repos removed
    removed_repos = before_repos - after_repos
    for repo_clean in removed_repos:
        cleanup_repo_tasks(uid=uid, repo_full_name=repo_clean, db=db)


# ============================================================================
# Cloud Firestore Triggers: Task Document Changes & Reranking
# ============================================================================


@firestore_fn.on_document_written(document="users/{uid}/tasks/{task_id}")
def on_task_written(event: firestore_fn.Event[firestore_fn.Change[firestore_fn.DocumentSnapshot | None]]) -> None:
    """
    Cloud Firestore trigger that monitors for changes to Task documents and triggers reranking
    whenever a task is newly created or updated with priority_needs_updated == True.
    """
    uid = event.params.get("uid")
    task_id = event.params.get("task_id")

    if event.data is None or event.data.after is None:
        return

    after_snap = event.data.after

    # If document does not exist after write, do nothing
    if not after_snap.exists:
        return

    after_data = after_snap.to_dict() or {}
    after_needs_update = after_data.get("priority_needs_updated", False)

    # Only trigger reranking if priority_needs_updated is True (prevents loop when ranker sets it to False)
    if not after_needs_update:
        return

    if uid and task_id:
        enqueue_task_ranking(uid=str(uid), task_id=str(task_id), function_name="rank_user_tasks", db=db)
