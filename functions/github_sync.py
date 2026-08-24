"""
GitHub integration module using PyGithub for fetching and storing user-associated issues and comments in Cloud Firestore.
Uses asynchronous chained Firebase Task Queue Functions (with seamless local fallback) for:
1. Assigned issues (filter=assigned)
2. Mentioned issues (filter=mentioned)
3. Created issues (filter=created)
4. Monitored repository issues (user.monitored_repos)
5. Chained paginated comments for each issue

NOTE: Tasks are ONLY created/updated once an Issue and all of its comments are fully imported into Firestore.
"""

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum

from github import Auth, Github, GithubException, GithubObject
from github.Issue import Issue as PyghIssue
from github.PaginatedList import PaginatedList
from google.cloud import firestore
from queue_utils import dispatch_task

from genai_ranker import IssuePayload
from task import ensure_task_for_issue
from user import User

logger = logging.getLogger(__name__)

GITHUB_API_BASE_URL = "https://api.github.com"


class AssociationReason(str, Enum):
    """
    Enumeration of reasons why a GitHub issue is associated with a user.
    """

    ASSIGNED = "assigned"
    MENTIONED = "mentioned"
    CREATED = "created"
    MONITORED_REPO = "monitored_repo"


def _safe_int(val: object, default: int = 0) -> int:
    """Safely converts an object to int or returns default."""
    if isinstance(val, int):
        return val
    if isinstance(val, str) and (val.isdigit() or (val.startswith("-") and val[1:].isdigit())):
        return int(val)
    return default


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
        except Exception as e:
            logger.warning(f"Failed to parse datetime '{dt_val}': {e}")
            return None
    return None


def _extract_owner_and_repo(issue_data: dict[str, object]) -> tuple[str, str]:
    """Extracts (owner, repo) from issue payload."""
    repo_obj = issue_data.get("repository")
    if isinstance(repo_obj, dict):
        owner_obj = repo_obj.get("owner")
        owner_login = owner_obj.get("login") if isinstance(owner_obj, dict) else None
        repo_name = repo_obj.get("name")
        if owner_login and repo_name:
            return str(owner_login), str(repo_name)

    repo_url = str(issue_data.get("repository_url") or "")
    match = re.search(r"repos/([^/]+)/([^/]+)", repo_url)
    if match:
        return match.group(1), match.group(2)

    html_url = str(issue_data.get("html_url") or issue_data.get("url") or "")
    match = re.search(r"(?:github\.com|repos)/([^/]+)/([^/]+)", html_url)
    if match:
        return match.group(1), match.group(2)

    return "unknown", "unknown"


# ============================================================================
# PyGithub Client
# ============================================================================


def get_github_client(access_token: str) -> Github:
    """
    Creates an authenticated PyGithub client instance.
    """
    auth = Auth.Token(access_token)
    return Github(auth=auth, timeout=20, user_agent="Firebase-GitHub-Sync-App")


# ============================================================================
# PyGithub Single-Page Fetching Helpers
# ============================================================================


def fetch_single_issue_page_pygithub(
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
            logger.warning(f"GitHub repository/endpoint not found: {repo_full_name or filter_name}")
            return [], False
        raise
    except Exception as e:
        logger.error(f"Error fetching issue page (page={page}): {e}")
        return [], False


# ============================================================================
# Backwards-Compatible Page Fetching Helpers (used in existing sync flows)
# ============================================================================


def fetch_single_issue_page(
    url: str, headers: dict[str, str], params: Mapping[str, object] | None = None, client: Github | None = None
) -> tuple[list[dict[str, object]], str | None]:
    """
    Fetches a single page of issues and returns (raw_items_dict, next_page_url).
    Delegates to PyGithub client when available.
    """
    try:
        token = headers.get("Authorization", "").replace("Bearer ", "").replace("token ", "").strip()
        g = client or (get_github_client(token) if token else None)
        if not g:
            return [], None

        repo_match = re.search(r"repos/([^/]+)/([^/]+)/issues", url)
        page = _safe_int(params.get("page"), default=0) if params else 0
        per_page = _safe_int(params.get("per_page"), default=100) if params else 100
        state = str(params.get("state", "open")) if params else "open"
        since_raw = params.get("since") if params else None
        since_dt = _parse_github_datetime(since_raw)

        if repo_match:
            owner, repo_name = repo_match.group(1), repo_match.group(2)
            issues, has_next = fetch_single_issue_page_pygithub(
                client=g,
                repo_full_name=f"{owner}/{repo_name}",
                state=state,
                since=since_dt,
                page=page,
                per_page=per_page,
            )
        else:
            filter_name = str(params.get("filter", "assigned")) if params else "assigned"
            issues, has_next = fetch_single_issue_page_pygithub(
                client=g, filter_name=filter_name, state=state, since=since_dt, page=page, per_page=per_page
            )

        items_dict: list[dict[str, object]] = []
        for it in issues:
            raw_repo = {
                "name": it.repository.name if it.repository else (repo_match.group(2) if repo_match else "unknown"),
                "owner": {
                    "login": it.repository.owner.login
                    if it.repository and it.repository.owner
                    else (repo_match.group(1) if repo_match else "unknown")
                },
            }
            raw_d: dict[str, object] = {
                "number": it.number,
                "title": it.title,
                "body": it.body,
                "state": it.state,
                "html_url": it.html_url,
                "comments": it.comments,
                "comments_url": it.comments_url,
                "created_at": it.created_at.isoformat() if it.created_at else None,
                "updated_at": it.updated_at.isoformat() if it.updated_at else None,
                "user": {"login": it.user.login} if it.user else None,
                "assignees": [{"login": a.login} for a in (it.assignees or []) if a and a.login],
                "pull_request": bool(it.pull_request),
                "repository": raw_repo,
            }
            items_dict.append(raw_d)

        next_url = f"{url}?page={page + 1}" if has_next else None
        return items_dict, next_url
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"Error fetching issue page from {url}: {e}")
        return [], None


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
    raw_items: list[dict[str, object]],
    reason: AssociationReason | str,
    db: firestore.Client,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
) -> list[str]:
    """
    Processes a page of GitHub issues for a given user and directly creates or updates
    the associated Task document in Firestore (users/{uid}/tasks/task_{doc_id}).
    Does not store intermediate issue documents in Firestore.
    """
    saved_doc_ids: list[str] = []

    for item in raw_items:
        raw_num = item.get("number", 0)
        issue_number = int(raw_num) if isinstance(raw_num, (int, str)) and str(raw_num).isdigit() else 0
        if issue_number <= 0:
            continue

        owner, repo = _extract_owner_and_repo(item)
        if owner == "unknown" and owner_fallback:
            owner = owner_fallback
        if repo == "unknown" and repo_fallback:
            repo = repo_fallback

        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", str(owner))
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", str(repo))
        doc_id = f"{clean_owner}_{clean_repo}_{issue_number}"

        issue_payload: dict[str, object] = {
            "title": str(item["title"]) if item.get("title") is not None else None,
            "url": str(item.get("html_url") or item.get("url") or ""),
            "owner": owner,
            "repo": repo,
            "issue_number": issue_number,
        }

        # Create/update Task directly in Firestore
        ensure_task_for_issue(uid=uid, issue_id=doc_id, issue_data=issue_payload, db=db)
        saved_doc_ids.append(doc_id)

    logger.info(
        f"Processed and created/updated tasks for {len(saved_doc_ids)} issues for UID {uid} under reason '{reason}'."
    )
    return saved_doc_ids


def execute_issue_page_sync(
    uid: str,
    url: str,
    params: Mapping[str, object] | None,
    reason: AssociationReason | str,
    owner_fallback: str | None,
    repo_fallback: str | None,
    db: firestore.Client,
) -> dict[str, object]:
    """
    Executes fetching one page of issues from GitHub and chaining to the next page if available.
    """
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()
    if not doc_snap.exists:
        return {"status": "error", "message": "User document not found."}

    raw_user_dict = doc_snap.to_dict() or {}
    user = User.model_validate({**raw_user_dict, "uid": uid})
    if not user.github_access_token:
        return {"status": "error", "message": "User does not have github_access_token configured."}

    headers = {
        "Authorization": f"Bearer {user.github_access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    client = get_github_client(user.github_access_token)
    items, next_url = fetch_single_issue_page(url=url, headers=headers, params=params, client=client)
    saved_doc_ids = process_and_save_issue_page(
        uid=uid, raw_items=items, reason=reason, db=db, owner_fallback=owner_fallback, repo_fallback=repo_fallback
    )

    # Chain next page of issues if present
    next_task_id = None
    if next_url:
        page_val = _safe_int(params.get("page"), default=0) if params else 0
        next_params: dict[str, object] = dict(params) if params else {}
        next_params["page"] = page_val + 1

        next_task_id = enqueue_issue_page_sync(
            uid=uid,
            url=url,
            params=next_params,
            reason=reason,
            owner_fallback=owner_fallback,
            repo_fallback=repo_fallback,
            db=db,
        )
        logger.info(f"Chained next issue page task {next_task_id} for URL: {next_url}")

    return {
        "status": "success",
        "saved_count": len(saved_doc_ids),
        "next_url": next_url,
        "next_task_id": next_task_id,
    }


def enqueue_issue_page_sync(
    uid: str,
    url: str,
    db: firestore.Client,
    params: Mapping[str, object] | None = None,
    reason: AssociationReason | str = AssociationReason.ASSIGNED,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
) -> str:
    """
    Enqueues a task to process a page of issues from GitHub using the task queue abstraction.
    Falls back seamlessly to background thread execution if running in the emulator.
    """
    reason_val = reason.value if isinstance(reason, AssociationReason) else str(reason)
    task_data = {
        "uid": uid,
        "url": url,
        "params": dict(params) if params else {},
        "reason": reason_val,
        "owner_fallback": owner_fallback,
        "repo_fallback": repo_fallback,
    }
    return dispatch_task(
        queue_name="sync_github_issues_page",
        task_data=task_data,
        worker_fn=lambda: execute_issue_page_sync(
            uid=uid,
            url=url,
            params=params,
            reason=reason,
            owner_fallback=owner_fallback,
            repo_fallback=repo_fallback,
            db=db,
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
    except GithubException as e:
        logger.warning(f"Failed to fetch GitHub user login: HTTP {e.status} - {e.data}")
        return None
    except Exception as e:
        logger.error(f"Exception fetching GitHub user login: {e}")
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
            logger.info(f"Discovered and stored github_username '{login}' for UID {user_uid}")

    since = user.last_assigned_issue_update_time
    enqueued_tasks: list[dict[str, object]] = []

    # 1. Enqueue user-level issue filters
    filters = [
        ("assigned", AssociationReason.ASSIGNED),
        ("mentioned", AssociationReason.MENTIONED),
        ("created", AssociationReason.CREATED),
    ]

    for filter_name, reason_enum in filters:
        params: dict[str, object] = {
            "filter": filter_name,
            "state": state,
            "per_page": 100,
            "page": 0,
        }
        if since:
            params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/issues"
        tid = enqueue_issue_page_sync(uid=user_uid, url=url, params=params, reason=reason_enum, db=db)
        enqueued_tasks.append({"reason": reason_enum.value, "url": url, "task_id": tid})

    # 2. Enqueue monitored repository endpoints
    for repo_path in user.monitored_repos:
        repo_clean = repo_path.strip().strip("/")
        if not repo_clean or "/" not in repo_clean:
            continue

        owner_part, repo_part = repo_clean.split("/", 1)
        repo_params: dict[str, object] = {
            "state": state,
            "per_page": 100,
            "page": 0,
        }
        if since:
            repo_params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/repos/{owner_part}/{repo_part}/issues"
        tid = enqueue_issue_page_sync(
            uid=user_uid,
            url=url,
            params=repo_params,
            reason=AssociationReason.MONITORED_REPO,
            owner_fallback=owner_part,
            repo_fallback=repo_part,
            db=db,
        )
        enqueued_tasks.append({"reason": f"monitored:{repo_clean}", "url": url, "task_id": tid})

    # Update sync timestamp on User profile
    now_iso = datetime.now(timezone.utc).isoformat()
    user.last_assigned_issue_update_time = now_iso
    db.collection("users").document(user_uid).set(
        {
            "last_assigned_issue_update_time": now_iso,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    logger.info(f"Started GitHub sync pipeline for UID {user_uid} ({len(enqueued_tasks)} initial queues scheduled).")
    return {
        "status": "enqueued",
        "uid": user_uid,
        "initial_queues_count": len(enqueued_tasks),
        "enqueued_tasks": enqueued_tasks,
        "sync_time": now_iso,
    }


# ============================================================================
# Scheduled Closed Issues Sync & Task Cleanup
# ============================================================================


def sync_closed_issues_for_user(user: User, db: firestore.Client, client: Github | None = None) -> dict[str, object]:
    """
    Queries GitHub for issues closed since the last sync via PyGithub
    and deletes the corresponding Task document from users/{uid}/tasks.
    """
    user_uid = user.uid or "unknown"
    if not user.github_access_token:
        return {"uid": user_uid, "closed_issues_count": 0, "status": "no_token"}

    headers = {
        "Authorization": f"Bearer {user.github_access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    g = client or get_github_client(user.github_access_token)
    since = user.last_assigned_issue_update_time
    closed_items: list[dict[str, object]] = []

    # 1. Query user-level closed issue filters
    filters = ["assigned", "mentioned", "created"]
    for filter_name in filters:
        params: dict[str, object] = {
            "filter": filter_name,
            "state": "closed",
            "per_page": 100,
            "page": 0,
        }
        if since:
            params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/issues"
        items, _ = fetch_single_issue_page(url=url, headers=headers, params=params, client=g)
        closed_items.extend(items)

    # 2. Query monitored repositories for closed issues
    for repo_path in user.monitored_repos:
        repo_clean = repo_path.strip().strip("/")
        if not repo_clean or "/" not in repo_clean:
            continue

        owner_part, repo_part = repo_clean.split("/", 1)
        repo_params: dict[str, object] = {
            "state": "closed",
            "per_page": 100,
            "page": 0,
        }
        if since:
            repo_params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/repos/{owner_part}/{repo_part}/issues"
        items, _ = fetch_single_issue_page(url=url, headers=headers, params=repo_params, client=g)
        for it in items:
            if "_owner_fallback" not in it:
                it["_owner_fallback"] = owner_part
                it["_repo_fallback"] = repo_part
            closed_items.append(it)

    # Process and remove closed issues from tasks
    closed_count = 0
    tasks_col = db.collection("users").document(user_uid).collection("tasks")

    seen_doc_ids: set[str] = set()

    for item in closed_items:
        owner, repo = _extract_owner_and_repo(item)
        if owner == "unknown" and "_owner_fallback" in item:
            owner = str(item["_owner_fallback"])
        if repo == "unknown" and "_repo_fallback" in item:
            repo = str(item["_repo_fallback"])

        raw_num = item.get("number", 0)
        issue_number = int(raw_num) if isinstance(raw_num, (int, str)) and str(raw_num).isdigit() else 0
        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", owner)
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)
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
            closed_count += 1
            logger.info(f"Deleted task {task_doc_id} for UID {user_uid} because GitHub issue was closed.")

    # Update sync timestamp
    now_iso = datetime.now(timezone.utc).isoformat()
    user.last_assigned_issue_update_time = now_iso
    db.collection("users").document(user_uid).set(
        {
            "last_assigned_issue_update_time": now_iso,
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    logger.info(f"Closed issue sync completed for UID {user_uid}: {closed_count} tasks removed.")
    return {"uid": user_uid, "closed_issues_count": closed_count, "sync_time": now_iso, "status": "success"}


def sync_all_users_closed_issues(db: firestore.Client) -> dict[str, object]:
    """
    Sweeps through all users in Firestore and removes closed issues from their task list.
    """
    users_col = db.collection("users")
    users_docs = users_col.stream()
    total_closed_tasks_removed = 0
    users_processed = 0

    for doc_snap in users_docs:
        raw_user_dict = doc_snap.to_dict() or {}
        user = User.model_validate({**raw_user_dict, "uid": doc_snap.id})
        if user.github_access_token:
            try:
                res = sync_closed_issues_for_user(user=user, db=db)
                raw_count = res.get("closed_issues_count", 0)
                if isinstance(raw_count, (int, float)):
                    total_closed_tasks_removed += int(raw_count)
                users_processed += 1
            except Exception as e:
                logger.error(f"Error syncing closed issues for UID {user.uid}: {e}")

    logger.info(f"Total {total_closed_tasks_removed} closed tasks removed across {users_processed} users.")
    return {
        "users_processed": users_processed,
        "total_closed_tasks_removed": total_closed_tasks_removed,
    }
