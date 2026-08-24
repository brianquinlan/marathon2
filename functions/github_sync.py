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
from task import ensure_task_for_issue
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
    Processes a page of GitHub issues for a given user and directly creates or updates
    the associated Task document in Firestore (users/{uid}/tasks/task_{doc_id}).
    Does not store intermediate issue documents in Firestore.
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


def start_user_github_sync(user: User, db: firestore.Client, state: str = "open") -> dict[str, object]:
    """
    Kicks off asynchronous, chained pagination tasks for all user issues:
    1. Assigned issues (filter=assigned)
    2. Mentioned issues (filter=mentioned)
    3. Created issues (filter=created)
    4. Repositories in user.monitored_repos
    """
    if not user.github_access_token:
        raise ValueError(
            f"User {user.uid or user.display_name or 'unknown'} does not have a github_access_token configured."
        )

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
        enqueue_issue_page_sync(
            uid=user_uid,
            db=db,
            filter_name=filter_name,
            state=state,
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
        enqueue_issue_page_sync(
            uid=user_uid,
            db=db,
            repo_full_name=repo_clean,
            state=state,
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
# Scheduled Closed Issues Sync & Task Cleanup
# ============================================================================


def sync_closed_issues_for_user(user: User, db: firestore.Client, client: Github | None = None) -> None:
    """
    Queries GitHub for issues closed since the last sync via PyGithub
    and deletes the corresponding Task document from users/{uid}/tasks.
    """
    user_uid = user.uid or "unknown"
    if not user.github_access_token:
        return

    g = client or get_github_client(user.github_access_token)
    closed_items: list[tuple[PyghIssue, str | None, str | None]] = []

    # 1. Query user-level closed issue filters with individual timestamps
    filter_timestamps: list[tuple[str, datetime | None]] = [
        ("assigned", user.last_assigned_sync),
        ("mentioned", user.last_mentioned_sync),
        ("created", user.last_created_sync),
    ]
    for filter_name, filter_since in filter_timestamps:
        items, _ = fetch_single_issue_page(
            client=g,
            filter_name=filter_name,
            state="closed",
            since=filter_since,
            page=0,
            per_page=100,
        )
        for it in items:
            closed_items.append((it, None, None))

    # 2. Query monitored repositories for closed issues with individual timestamps
    for repo_path, repo_since in user.monitored_repos.items():
        repo_clean = repo_path.strip().strip("/")
        if not repo_clean or "/" not in repo_clean:
            continue

        owner_part, repo_part = repo_clean.split("/", 1)
        items, _ = fetch_single_issue_page(
            client=g,
            repo_full_name=repo_clean,
            state="closed",
            since=repo_since,
            page=0,
            per_page=100,
        )
        for it in items:
            closed_items.append((it, owner_part, repo_part))

    # Process and remove closed issues from tasks
    tasks_col = db.collection("users").document(user_uid).collection("tasks")
    seen_doc_ids: set[str] = set()

    for item, owner_fallback, repo_fallback in closed_items:
        owner = (
            item.repository.owner.login if item.repository and item.repository.owner else (owner_fallback or "unknown")
        )
        repo = item.repository.name if item.repository else (repo_fallback or "unknown")

        issue_number = item.number
        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", str(owner))
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", str(repo))
        doc_id = f"{clean_owner}_{clean_repo}_{issue_number}"

        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)

        # Delete corresponding task if it exists
        task_doc_id = f"task_{doc_id}"
        task_ref = tasks_col.document(task_doc_id)
        task_snap = task_ref.get()
        if task_snap.exists:
            task_ref.delete()

    # Update sync timestamps
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


def sync_all_users_closed_issues(db: firestore.Client) -> None:
    """
    Sweeps through all users in Firestore and removes closed issues from their task list.
    """
    users_col = db.collection("users")
    users_docs = users_col.stream()

    for doc_snap in users_docs:
        raw_user_dict = doc_snap.to_dict() or {}
        user = User.model_validate({**raw_user_dict, "uid": doc_snap.id})
        if user.github_access_token:
            try:
                sync_closed_issues_for_user(user=user, db=db)
            except Exception:
                pass
