"""
GitHub integration module using PyGithub for fetching and storing user-associated issues and comments in Cloud Firestore.
Uses asynchronous chained Firebase Task Queue Functions (with seamless local fallback) for:
1. Assigned issues (filter=assigned)
2. Mentioned issues (filter=mentioned)
3. Created issues (filter=created)
4. Monitored repository issues (user.monitored_repos)

NOTE: Tasks are ONLY created/updated once an Issue and all of its comments are fully imported into Firestore.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from github import Auth, Github, GithubException, GithubObject
from github.Issue import Issue as PyghIssue
from github.PaginatedList import PaginatedList
from google.cloud import firestore
from queue_utils import dispatch_task

from genai_ranker import IssuePayload
from task import delete_task_for_issue, ensure_task_for_issue
from user import User


def _parse_github_datetime(dt_val: object) -> datetime | None:
    """Parses ISO 8601 strings or timestamp objects into UTC datetime objects."""
    if not dt_val:
        return None
    if isinstance(dt_val, datetime):
        return dt_val if dt_val.tzinfo else dt_val.replace(tzinfo=timezone.utc)
    if isinstance(dt_val, str):
        try:
            clean_str = dt_val.replace("Z", "+00:00")
            return datetime.fromisoformat(clean_str)
        except Exception:
            return None
    return None


# ============================================================================
# PyGithub Client
# ============================================================================


def get_github_client(access_token: str, per_page: int = 100) -> Github:
    """
    Creates an authenticated PyGithub client instance configured with default per_page.
    """
    auth = Auth.Token(access_token)
    return Github(auth=auth, per_page=per_page, timeout=20, user_agent="Firebase-GitHub-Sync-App")


# ============================================================================
# PyGithub Single-Page Fetching Helpers
# ============================================================================


def fetch_single_issue_page(
    client: Github,
    filter_name: str | None = None,
    repo_full_name: str | None = None,
    state: str = "open",
    since: datetime | None = None,
    page: int = 0,
    per_page: int = 100,
) -> tuple[list[PyghIssue], bool]:
    """
    Fetches a single page (0-indexed) of issues using PyGithub PaginatedList.
    Returns (items, has_next_page).
    """
    try:
        client.per_page = per_page
        if repo_full_name:
            repo = client.get_repo(repo_full_name)
            paginated: PaginatedList[PyghIssue] = repo.get_issues(
                state=state, since=since or GithubObject.NotSet, sort="updated", direction="desc"
            )
        else:
            paginated = client.get_user().get_issues(
                filter=filter_name or "assigned",
                state=state,
                since=since or GithubObject.NotSet,
                sort="updated",
                direction="desc",
            )
        items = list(paginated.get_page(page))
        has_next = len(items) >= per_page
        return items, has_next
    except GithubException as e:
        if e.status in (401, 403):
            raise PermissionError(f"GitHub API authorization error ({e.status}): {e.data}") from e
        elif e.status == 404:
            return [], False
        raise
    except Exception:
        return [], False


# ============================================================================
# In-Memory Fetcher & Page Processing
# ============================================================================


def fetch_issue_in_memory(
    access_token: str,
    owner: str,
    repo: str,
    issue_number: int,
    client: Github | None = None,
) -> IssuePayload:
    """
    Fetches the raw issue and comments JSON directly from GitHub into memory via PyGithub.
    Used on-demand by the ranker without storing intermediate issue documents in Firestore.
    """
    g = client or get_github_client(access_token)
    pygh_repo = g.get_repo(f"{owner}/{repo}")
    pygh_issue = pygh_repo.get_issue(number=issue_number)

    raw_issue: dict[str, object] = pygh_issue.raw_data or {}
    raw_comments: list[dict[str, object]] = [c.raw_data for c in pygh_issue.get_comments() if c and c.raw_data]

    return IssuePayload(issue=raw_issue, comments=raw_comments)


def process_and_save_issue_page(
    uid: str,
    issues: list[PyghIssue],
    db: firestore.Client,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
    source: str | None = None,
) -> None:
    """
    Processes a page of GitHub issues for a given user:
    - If the issue is closed: deletes the corresponding Task document from Firestore.
    - If the issue is open: creates or updates the associated Task document (users/{uid}/tasks/task_{doc_id}).
    """
    for issue in issues:
        issue_number = issue.number
        if issue_number <= 0:
            continue

        owner = (
            issue.repository.owner.login
            if issue.repository and issue.repository.owner
            else (owner_fallback or "unknown")
        )
        repo = issue.repository.name if issue.repository else (repo_fallback or "unknown")

        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", str(owner))
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", str(repo))
        doc_id = f"{clean_owner}_{clean_repo}_{issue_number}"

        # If the issue is closed, delete the task from Firestore
        if getattr(issue, "state", "open") == "closed":
            delete_task_for_issue(uid=uid, issue_id=doc_id, db=db)
            continue

        issue_payload: dict[str, object] = {
            "title": issue.title,
            "url": issue.html_url or "",
            "owner": owner,
            "repo": repo,
            "issue_number": issue_number,
        }

        # Create/update Task directly in Firestore
        ensure_task_for_issue(
            uid=uid,
            issue_id=doc_id,
            issue_data=issue_payload,
            db=db,
            source=source,
        )


def execute_issue_page_sync(
    uid: str,
    db: firestore.Client,
    filter_name: str | None = None,
    repo_full_name: str | None = None,
    state: str = "open",
    since: datetime | str | None = None,
    page: int = 0,
    per_page: int = 100,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
    client: Github | None = None,
) -> None:
    """
    Executes fetching one page of issues from GitHub via PyGithub and chaining to the next page if available.
    """
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()
    if not doc_snap.exists:
        return

    raw_user_dict = doc_snap.to_dict() or {}
    user = User.model_validate({**raw_user_dict, "uid": uid})
    if not user.github_access_token:
        return

    g = client or get_github_client(user.github_access_token)
    since_dt = since if isinstance(since, datetime) else _parse_github_datetime(since)
    items, has_next = fetch_single_issue_page(
        client=g,
        filter_name=filter_name,
        repo_full_name=repo_full_name,
        state=state,
        since=since_dt,
        page=page,
        per_page=per_page,
    )
    source = "monitored" if repo_full_name else (filter_name or "assigned")
    process_and_save_issue_page(
        uid=uid,
        issues=items,
        db=db,
        owner_fallback=owner_fallback,
        repo_fallback=repo_fallback,
        source=source,
    )

    # Chain next page of issues if present
    if has_next:
        enqueue_issue_page_sync(
            uid=uid,
            db=db,
            filter_name=filter_name,
            repo_full_name=repo_full_name,
            state=state,
            since=since,
            page=page + 1,
            per_page=per_page,
            owner_fallback=owner_fallback,
            repo_fallback=repo_fallback,
        )


def enqueue_issue_page_sync(
    uid: str,
    db: firestore.Client,
    filter_name: str | None = None,
    repo_full_name: str | None = None,
    state: str = "open",
    since: datetime | str | None = None,
    page: int = 0,
    per_page: int = 100,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
) -> None:
    """
    Enqueues a task to process a page of issues from GitHub using the task queue abstraction.
    Falls back seamlessly to background thread execution if running in the emulator.
    """
    since_str = since.isoformat() if isinstance(since, datetime) else (str(since) if since is not None else None)
    task_data: dict[str, object] = {
        "uid": uid,
        "filter_name": filter_name,
        "repo_full_name": repo_full_name,
        "state": state,
        "since": since_str,
        "page": page,
        "per_page": per_page,
        "owner_fallback": owner_fallback,
        "repo_fallback": repo_fallback,
    }
    dispatch_task(
        queue_name="sync_github_issues_page",
        task_data=task_data,
        worker_fn=lambda: execute_issue_page_sync(
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
        ),
    )


def fetch_github_user_login(access_token: str) -> str | None:
    """
    Fetches the authenticated user's GitHub username (login) using their access token via PyGithub.
    """
    try:
        client = get_github_client(access_token)
        user = client.get_user()
        login = user.login
        if login:
            return str(login)
        return None
    except Exception:
        return None


# ============================================================================
# Initial Sync Dispatcher
# ============================================================================


def start_user_github_sync(user: User, db: firestore.Client, state: str | None = None) -> dict[str, object]:
    """
    Kicks off asynchronous, chained pagination tasks for all user issues:
    1. Assigned issues (filter=assigned)
    2. Mentioned issues (filter=mentioned)
    3. Created issues (filter=created)
    4. Repositories in user.monitored_repos

    If state is None (default):
    - If since is None (initial sync): state="open" to avoid paginating historical closed issues.
    - If since is not None (incremental sync): state="all" to capture both open updates and newly closed issues.
    """
    if not user.github_access_token:
        raise ValueError(f"User {user.uid or 'unknown'} does not have a github_access_token configured.")

    user_uid = user.uid or "unknown"

    # Discover and store github_username if not already populated
    if not user.github_username and user.github_access_token:
        login = fetch_github_user_login(user.github_access_token)
        if login:
            user.github_username = login
            db.collection("users").document(user_uid).set(
                {
                    "github_username": login,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )

    enqueued_tasks: list[dict[str, object]] = []

    # 1. Enqueue user-level issue filters with individual timestamps
    filter_timestamps: list[tuple[str, datetime | None]] = [
        ("assigned", user.last_assigned_sync),
        ("mentioned", user.last_mentioned_sync),
        ("created", user.last_created_sync),
    ]
    for filter_name, filter_since in filter_timestamps:
        filter_state = state if state is not None else ("all" if filter_since is not None else "open")
        enqueue_issue_page_sync(
            uid=user_uid,
            db=db,
            filter_name=filter_name,
            state=filter_state,
            since=filter_since,
            page=0,
            per_page=100,
        )
        enqueued_tasks.append({"filter": filter_name})

    # 2. Enqueue monitored repository endpoints with individual timestamps
    for repo_path, repo_since in user.monitored_repos.items():
        repo_clean = repo_path.strip().strip("/")
        if not repo_clean or "/" not in repo_clean:
            continue

        owner_part, repo_part = repo_clean.split("/", 1)
        repo_state = state if state is not None else ("all" if repo_since is not None else "open")
        enqueue_issue_page_sync(
            uid=user_uid,
            db=db,
            repo_full_name=repo_clean,
            state=repo_state,
            since=repo_since,
            page=0,
            per_page=100,
            owner_fallback=owner_part,
            repo_fallback=repo_part,
        )
        enqueued_tasks.append({"repo": repo_clean})

    # Update sync timestamps on User profile
    now = datetime.now(timezone.utc)
    user.last_assigned_sync = now
    user.last_mentioned_sync = now
    user.last_created_sync = now
    for repo_path in list(user.monitored_repos.keys()):
        user.monitored_repos[repo_path] = now

    db.collection("users").document(user_uid).set(
        {
            "last_assigned_sync": now,
            "last_mentioned_sync": now,
            "last_created_sync": now,
            "monitored_repos": user.monitored_repos,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return {
        "status": "enqueued",
        "uid": user_uid,
        "initial_queues_count": len(enqueued_tasks),
        "enqueued_tasks": enqueued_tasks,
        "sync_time": now.isoformat(),
    }


# ============================================================================
# Periodic User Sync
# ============================================================================


def sync_user_periodic(uid: str, db: firestore.Client) -> dict[str, object]:
    """
    Executes a periodic sync cycle for a single user:
    Syncs open and closed issue updates (state="all") across assigned, mentioned,
    created, and monitored repositories, updating tasks and timestamps.
    """
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()
    if not doc_snap.exists:
        return {"status": "skipped", "reason": "user_not_found"}

    raw_user_dict = doc_snap.to_dict() or {}
    user = User.model_validate({**raw_user_dict, "uid": uid})
    if not user.github_access_token:
        return {"status": "skipped", "reason": "no_github_token"}

    sync_result = start_user_github_sync(user=user, db=db)

    return {
        "status": "success",
        "uid": uid,
        "sync_result": sync_result,
    }


def enqueue_user_periodic_sync(uid: str, db: firestore.Client) -> None:
    """
    Enqueues a task to run the periodic sync for a single user using the task queue abstraction.
    """
    dispatch_task(
        queue_name="sync_user_periodic_task",
        task_data={"uid": uid},
        worker_fn=lambda: sync_user_periodic(uid=uid, db=db),
    )
