"""
GitHub integration module for fetching and storing user-associated issues and comments in Cloud Firestore.
Uses asynchronous chained Firebase Task Queue Functions (with seamless local emulator fallback) for:
1. Assigned issues (filter=assigned)
2. Mentioned issues (filter=mentioned)
3. Created issues (filter=created)
4. Monitored repository issues (user.monitored_repos)
5. Chained paginated comments for each issue

NOTE: Tasks are ONLY created/updated once an Issue and all of its comments are fully imported into Firestore.
"""

from datetime import datetime, timezone
from enum import Enum
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import logging
import requests
from pydantic import BaseModel, Field, ConfigDict
from google.cloud import firestore
import firebase_admin
from firebase_admin import functions as admin_functions

from user import User
from task import ensure_task_for_issue

logger = logging.getLogger(__name__)

GITHUB_API_BASE_URL = "https://api.github.com"


class IssueType(str, Enum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"


class AssociationReason(str, Enum):
    """
    Enumeration of reasons why a GitHub issue is associated with a user.
    """
    ASSIGNED = "assigned"
    MENTIONED = "mentioned"
    CREATED = "created"
    MONITORED_REPO = "monitored_repo"


class Comment(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int
    user_login: str
    body: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_api_dict(cls, data: Dict[str, Any]) -> "Comment":
        return cls(
            id=data["id"],
            user_login=data.get("user", {}).get("login", "unknown"),
            body=data.get("body") or "",
            created_at=_parse_github_datetime(data.get("created_at")),
            updated_at=_parse_github_datetime(data.get("updated_at")),
        )


class Issue(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    owner: str
    repo: str
    issue_number: int
    issue_type: IssueType
    comments_url: str
    number: int
    body: Optional[str] = None
    user_login: str
    assignee_logins: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    state: str = "open"
    last_comment_update_time: Optional[datetime] = None
    comments: List[Comment] = Field(default_factory=list)
    association_reasons: List[AssociationReason] = Field(default_factory=list)
    title: Optional[str] = None

    @property
    def doc_id(self) -> str:
        """Unique document ID for Firestore: {owner}_{repo}_{number}"""
        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", self.owner)
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", self.repo)
        return f"{clean_owner}_{clean_repo}_{self.issue_number}"

    @property
    def created_atr(self) -> datetime:
        return self.created_at


def _parse_github_datetime(dt_val: Optional[Any]) -> Optional[datetime]:
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


def _extract_owner_and_repo(issue_data: Dict[str, Any]) -> Tuple[str, str]:
    """Extracts (owner, repo) from issue payload."""
    repo_obj = issue_data.get("repository")
    if isinstance(repo_obj, dict):
        owner = repo_obj.get("owner", {}).get("login")
        repo = repo_obj.get("name")
        if owner and repo:
            return owner, repo

    repo_url = issue_data.get("repository_url") or ""
    match = re.search(r"repos/([^/]+)/([^/]+)", repo_url)
    if match:
        return match.group(1), match.group(2)

    html_url = issue_data.get("html_url") or issue_data.get("url") or ""
    match = re.search(r"(?:github\.com|repos)/([^/]+)/([^/]+)", html_url)
    if match:
        return match.group(1), match.group(2)

    return "unknown", "unknown"


# ============================================================================
# Single-Page Fetching Helpers (with Next Link parsing)
# ============================================================================

def fetch_single_issue_page(
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Fetches a single page of issues from GitHub and returns (items, next_page_url).
    """
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code == 401:
            raise PermissionError("Invalid or expired GitHub access token.")
        elif resp.status_code == 403:
            raise PermissionError(f"GitHub API rate limit exceeded or forbidden: {resp.text}")
        elif resp.status_code == 404:
            logger.warning(f"GitHub endpoint not found: {url}")
            return [], None

        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else []
        next_url = resp.links.get("next", {}).get("url")
        return items, next_url
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"Error fetching issue page from {url}: {e}")
        return [], None


def fetch_single_comment_page(
    comments_url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None
) -> Tuple[List[Comment], Optional[str]]:
    """
    Fetches a single page of comments from GitHub and returns (comments, next_page_url).
    """
    try:
        resp = requests.get(comments_url, headers=headers, params=params, timeout=15)
        if resp.status_code in (401, 403):
            raise PermissionError("GitHub API authorization error.")
        elif resp.status_code == 404:
            return [], None

        resp.raise_for_status()
        data = resp.json()
        comments = []
        if isinstance(data, list):
            for c in data:
                comments.append(Comment.from_api_dict(c))

        next_url = resp.links.get("next", {}).get("url")
        return comments, next_url
    except Exception as e:
        logger.error(f"Error fetching comments from {comments_url}: {e}")
        return [], None


# ============================================================================
# Page Processing & Firestore Persistence
# ============================================================================

def process_and_save_issue_page(
    uid: str,
    raw_items: List[Dict[str, Any]],
    reason: str,
    db: firestore.Client,
    owner_fallback: Optional[str] = None,
    repo_fallback: Optional[str] = None
) -> List[str]:
    """
    Parses a page of raw GitHub issue dicts and merges them with existing Firestore documents.
    Note: Tasks are only created here if the issue has 0 comments (otherwise created when comments finish).
    """
    if not raw_items:
        return []

    issues_col = db.collection("users").document(uid).collection("issues")
    saved_doc_ids: List[str] = []

    for item in raw_items:
        owner, repo = _extract_owner_and_repo(item)
        if owner == "unknown" and owner_fallback:
            owner = owner_fallback
        if repo == "unknown" and repo_fallback:
            repo = repo_fallback

        issue_number = item.get("number", 0)
        is_pr = "pull_request" in item and item["pull_request"] is not None
        issue_type = IssueType.PULL_REQUEST if is_pr else IssueType.ISSUE

        comments_url = item.get("comments_url") or f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        created_at = _parse_github_datetime(item.get("created_at")) or datetime.now(timezone.utc)
        updated_at = _parse_github_datetime(item.get("updated_at")) or datetime.now(timezone.utc)

        assignees = [
            a.get("login")
            for a in item.get("assignees", [])
            if isinstance(a, dict) and a.get("login")
        ]
        if not assignees and item.get("assignee"):
            assignees = [item["assignee"].get("login")]

        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", owner)
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)
        doc_id = f"{clean_owner}_{clean_repo}_{issue_number}"

        # Fetch existing document to merge association_reasons and preserve existing comments
        doc_ref = issues_col.document(doc_id)
        doc_snap = doc_ref.get()

        parsed_reason = AssociationReason(reason) if isinstance(reason, str) and reason in AssociationReason._value2member_map_ else reason
        reasons = {parsed_reason}
        existing_comments: List[Comment] = []
        last_comment_time = None

        if doc_snap.exists:
            existing_data = doc_snap.to_dict() or {}
            for r in existing_data.get("association_reasons") or []:
                if isinstance(r, str) and r in AssociationReason._value2member_map_:
                    reasons.add(AssociationReason(r))
                else:
                    reasons.add(r)
            existing_comments = [
                Comment.model_validate(c)
                for c in existing_data.get("comments", [])
                if isinstance(c, dict)
            ]
            last_comment_time = _parse_github_datetime(existing_data.get("last_comment_update_time"))

        issue = Issue(
            url=item.get("html_url") or item.get("url") or "",
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            number=issue_number,
            title=item.get("title"),
            issue_type=issue_type,
            state=item.get("state", "open"),
            comments_url=comments_url,
            body=item.get("body"),
            user_login=item.get("user", {}).get("login", "unknown"),
            assignee_logins=assignees,
            created_at=created_at,
            updated_at=updated_at,
            last_comment_update_time=last_comment_time,
            comments=existing_comments,
            association_reasons=sorted(list(reasons)),
        )

        issue_dict = issue.model_dump(mode="json")
        issue_dict["synced_at"] = firestore.SERVER_TIMESTAMP
        doc_ref.set(issue_dict, merge=True)

        # Only create Task immediately if the issue has 0 comments (already fully imported)
        comments_count = item.get("comments", 0)
        if comments_count == 0:
            ensure_task_for_issue(
                uid=uid,
                issue_id=doc_id,
                issue_data=issue.model_dump(mode="json"),
                db=db
            )
            logger.info(f"Created/updated Task for issue {doc_id} with 0 comments.")

        saved_doc_ids.append(doc_id)

    logger.info(f"Processed and saved {len(saved_doc_ids)} issues for UID {uid} under reason '{reason}'.")
    return saved_doc_ids


def process_and_save_comment_page(
    uid: str,
    issue_doc_id: str,
    new_comments: List[Comment],
    db: firestore.Client,
    is_last_page: bool = False
) -> int:
    """
    Appends or merges a page of comments into the Firestore issue document,
    and updates last_comment_update_time.
    When is_last_page is True, creates/updates the Task since the Issue and all comments are fully imported.
    """
    issue_ref = db.collection("users").document(uid).collection("issues").document(issue_doc_id)
    doc_snap = issue_ref.get()

    if not doc_snap.exists:
        logger.warning(f"Cannot save comments: issue {issue_doc_id} not found for UID {uid}")
        return 0

    existing_data = doc_snap.to_dict() or {}
    existing_comments = [
        Comment.model_validate(c)
        for c in existing_data.get("comments", [])
        if isinstance(c, dict)
    ]

    # Deduplicate comments by ID
    comments_by_id: Dict[int, Comment] = {c.id: c for c in existing_comments}
    for nc in new_comments:
        comments_by_id[nc.id] = nc

    all_comments = sorted(
        list(comments_by_id.values()),
        key=lambda c: c.created_at or datetime.min.replace(tzinfo=timezone.utc)
    )

    last_comment_time = max(
        [c.updated_at or c.created_at for c in all_comments if c.updated_at or c.created_at],
        default=None
    )

    update_payload: Dict[str, Any] = {
        "comments": [c.model_dump(mode="json") for c in all_comments],
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if last_comment_time:
        update_payload["last_comment_update_time"] = last_comment_time.isoformat()

    issue_ref.set(update_payload, merge=True)
    logger.info(f"Saved {len(new_comments)} comments for issue {issue_doc_id} (Total: {len(all_comments)}).")

    # If all comments have been imported, create/update the associated Task!
    if is_last_page:
        updated_issue_data = {**existing_data, **update_payload}
        ensure_task_for_issue(
            uid=uid,
            issue_id=issue_doc_id,
            issue_data=updated_issue_data,
            db=db
        )
        logger.info(f"Issue {issue_doc_id} and all comments fully imported. Created/updated Task.")

    return len(new_comments)


# ============================================================================
# Execution & Task Queue Enqueuers (Chaining Pagination)
# ============================================================================

def execute_issue_page_sync(
    uid: str,
    url: str,
    params: Optional[Dict[str, Any]],
    reason: str,
    owner_fallback: Optional[str],
    repo_fallback: Optional[str],
    db: firestore.Client
) -> Dict[str, Any]:
    """
    Executes fetching one page of issues and scheduling comment sync for each.
    """
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()
    if not doc_snap.exists:
        return {"status": "error", "message": "User document not found."}

    user = User.model_validate({**(doc_snap.to_dict() or {}), "uid": uid})
    if not user.github_access_token:
        return {"status": "error", "message": "User does not have github_access_token configured."}

    headers = {
        "Authorization": f"Bearer {user.github_access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    items, next_url = fetch_single_issue_page(url=url, headers=headers, params=params)
    saved_doc_ids = process_and_save_issue_page(
        uid=uid,
        raw_items=items,
        reason=reason,
        db=db,
        owner_fallback=owner_fallback,
        repo_fallback=repo_fallback
    )

    # Enqueue comment fetching tasks for each issue in this page
    for item in items:
        issue_number = item.get("number", 0)
        comments_url = item.get("comments_url")
        if not comments_url:
            continue

        owner, repo = _extract_owner_and_repo(item)
        if owner == "unknown" and owner_fallback:
            owner = owner_fallback
        if repo == "unknown" and repo_fallback:
            repo = repo_fallback
        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", str(owner))
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", str(repo))
        doc_id = f"{clean_owner}_{clean_repo}_{issue_number}"

        comments_count = item.get("comments", 0)
        if comments_count > 0:
            enqueue_comment_page_sync(
                uid=uid,
                issue_doc_id=doc_id,
                comments_url=comments_url,
                params={"per_page": 100, "since": user.last_assigned_issue_update_time} if user.last_assigned_issue_update_time else {"per_page": 100},
                db=db
            )

    # Chain next page of issues if present
    next_task_id = None
    if next_url:
        next_task_id = enqueue_issue_page_sync(
            uid=uid,
            url=next_url,
            params=None,
            reason=reason,
            owner_fallback=owner_fallback,
            repo_fallback=repo_fallback,
            db=db
        )
        logger.info(f"Chained next issue page task {next_task_id} for URL: {next_url}")

    return {
        "status": "success",
        "saved_count": len(saved_doc_ids),
        "next_url": next_url,
        "next_task_id": next_task_id,
    }


def execute_comment_page_sync(
    uid: str,
    issue_doc_id: str,
    comments_url: str,
    params: Optional[Dict[str, Any]],
    db: firestore.Client
) -> Dict[str, Any]:
    """
    Executes fetching one page of comments and chaining if necessary.
    """
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()
    if not doc_snap.exists:
        return {"status": "error", "message": "User document not found."}

    user = User.model_validate({**(doc_snap.to_dict() or {}), "uid": uid})
    if not user.github_access_token:
        return {"status": "error", "message": "User does not have github_access_token configured."}

    headers = {
        "Authorization": f"Bearer {user.github_access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    comments, next_url = fetch_single_comment_page(
        comments_url=comments_url,
        headers=headers,
        params=params
    )

    saved_count = process_and_save_comment_page(
        uid=uid,
        issue_doc_id=issue_doc_id,
        new_comments=comments,
        db=db,
        is_last_page=(next_url is None)
    )

    next_task_id = None
    if next_url:
        next_task_id = enqueue_comment_page_sync(
            uid=uid,
            issue_doc_id=issue_doc_id,
            comments_url=next_url,
            params=None,
            db=db
        )
        logger.info(f"Chained next comments page task {next_task_id} for issue {issue_doc_id}")

    return {
        "status": "success",
        "saved_comments_count": saved_count,
        "next_url": next_url,
        "next_task_id": next_task_id,
    }


def enqueue_issue_page_sync(
    uid: str,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    reason: Union[AssociationReason, str] = AssociationReason.ASSIGNED,
    owner_fallback: Optional[str] = None,
    repo_fallback: Optional[str] = None,
    db: Optional[firestore.Client] = None
) -> Optional[str]:
    """
    Enqueues a task to process a page of issues from GitHub using Firebase task_queue.
    Falls back seamlessly to background thread execution if task queue is not available in local environment.
    """
    try:
        queue = admin_functions.task_queue("sync_github_issues_page")
        reason_val = reason.value if isinstance(reason, AssociationReason) else str(reason)
        task_data = {
            "uid": uid,
            "url": url,
            "params": params or {},
            "reason": reason_val,
            "owner_fallback": owner_fallback,
            "repo_fallback": repo_fallback,
        }
        task_id = queue.enqueue(task_data, opts=admin_functions.TaskOptions(dispatch_deadline_seconds=300))
        logger.info(f"Enqueued sync_github_issues_page task '{task_id}' for UID {uid}, url={url}")
        return task_id
    except Exception as e:
        logger.warning(f"Firebase task_queue.enqueue exception ({e}). Handling fallback dispatch.")
        if db is not None:
            def _worker():
                try:
                    execute_issue_page_sync(
                        uid=uid,
                        url=url,
                        params=params,
                        reason=reason,
                        owner_fallback=owner_fallback,
                        repo_fallback=repo_fallback,
                        db=db
                    )
                except Exception as inner_e:
                    logger.error(f"Background thread execution error in execute_issue_page_sync: {inner_e}")

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            return "thread_dispatched"
        return None


def enqueue_comment_page_sync(
    uid: str,
    issue_doc_id: str,
    comments_url: str,
    params: Optional[Dict[str, Any]] = None,
    db: Optional[firestore.Client] = None
) -> Optional[str]:
    """
    Enqueues a task to process a page of comments for a specific issue using Firebase task_queue.
    Falls back seamlessly to background thread execution if task queue is not available in local environment.
    """
    try:
        queue = admin_functions.task_queue("sync_issue_comments_page")
        task_data = {
            "uid": uid,
            "issue_doc_id": issue_doc_id,
            "comments_url": comments_url,
            "params": params or {"per_page": 100},
        }
        task_id = queue.enqueue(task_data, opts=admin_functions.TaskOptions(dispatch_deadline_seconds=300))
        logger.info(f"Enqueued sync_issue_comments_page task '{task_id}' for issue {issue_doc_id}")
        return task_id
    except Exception as e:
        logger.warning(f"Firebase task_queue.enqueue exception ({e}). Handling fallback dispatch.")
        if db is not None:
            def _worker():
                try:
                    execute_comment_page_sync(
                        uid=uid,
                        issue_doc_id=issue_doc_id,
                        comments_url=comments_url,
                        params=params,
                        db=db
                    )
                except Exception as ex:
                    logger.error(f"Error in fallback execute_comment_page_sync: {ex}")

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
        return "fallback_dispatched"


def fetch_github_user_login(access_token: str) -> Optional[str]:
    """
    Fetches the authenticated user's GitHub username (login) using their access token.
    Calls GET https://api.github.com/user.
    """
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Firebase-GitHub-Sync-App",
        }
        resp = requests.get(f"{GITHUB_API_BASE_URL}/user", headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("login")
        logger.warning(f"Failed to fetch GitHub user login: HTTP {resp.status_code} - {resp.text}")
        return None
    except Exception as e:
        logger.error(f"Exception fetching GitHub user login: {e}")
        return None


# ============================================================================
# Initial Sync Dispatcher
# ============================================================================

def start_user_github_sync(
    user: User,
    db: firestore.Client,
    state: str = "open"
) -> Dict[str, Any]:
    """
    Kicks off asynchronous, chained pagination tasks for all user issues:
    1. Assigned issues (filter=assigned)
    2. Mentioned issues (filter=mentioned)
    3. Created issues (filter=created)
    4. Repositories in user.monitored_repos
    """
    if not user.github_access_token:
        raise ValueError(f"User {user.uid or user.display_name or 'unknown'} does not have a github_access_token configured.")

    # Discover and store github_username if not already populated
    if not user.github_username and user.github_access_token:
        login = fetch_github_user_login(user.github_access_token)
        if login:
            user.github_username = login
            db.collection("users").document(user.uid).set({
                "github_username": login,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }, merge=True)
            logger.info(f"Discovered and stored github_username '{login}' for UID {user.uid}")

    since = user.last_assigned_issue_update_time
    enqueued_tasks = []

    # 1. Enqueue user-level issue filters
    filters = [
        ("assigned", AssociationReason.ASSIGNED),
        ("mentioned", AssociationReason.MENTIONED),
        ("created", AssociationReason.CREATED),
    ]

    for filter_name, reason_enum in filters:
        params: Dict[str, Any] = {
            "filter": filter_name,
            "state": state,
            "per_page": 100,
        }
        if since:
            params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/issues"
        tid = enqueue_issue_page_sync(
            uid=user.uid,
            url=url,
            params=params,
            reason=reason_enum,
            db=db
        )
        enqueued_tasks.append({"reason": reason_enum.value, "url": url, "task_id": tid})

    # 2. Enqueue monitored repository endpoints
    for repo_path in user.monitored_repos:
        repo_clean = repo_path.strip().strip("/")
        if not repo_clean or "/" not in repo_clean:
            continue

        owner_part, repo_part = repo_clean.split("/", 1)
        params = {
            "state": state,
            "per_page": 100,
        }
        if since:
            params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/repos/{owner_part}/{repo_part}/issues"
        tid = enqueue_issue_page_sync(
            uid=user.uid,
            url=url,
            params=params,
            reason=AssociationReason.MONITORED_REPO,
            owner_fallback=owner_part,
            repo_fallback=repo_part,
            db=db
        )
        enqueued_tasks.append({"reason": f"monitored:{repo_clean}", "url": url, "task_id": tid})

    # Update sync timestamp on User profile
    now_iso = datetime.now(timezone.utc).isoformat()
    user.last_assigned_issue_update_time = now_iso
    db.collection("users").document(user.uid).set({
        "last_assigned_issue_update_time": now_iso,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)

    logger.info(f"Started GitHub sync pipeline for UID {user.uid} ({len(enqueued_tasks)} initial queues scheduled).")
    return {
        "status": "enqueued",
        "uid": user.uid,
        "initial_queues_count": len(enqueued_tasks),
        "enqueued_tasks": enqueued_tasks,
        "sync_time": now_iso,
    }


def get_user_stored_issues(
    uid: str,
    db: firestore.Client,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Retrieves stored issues from Firestore for a given user UID.
    """
    issues_ref = db.collection("users").document(uid).collection("issues")
    docs = issues_ref.limit(limit).stream()

    results = []
    for doc_snap in docs:
        data = doc_snap.to_dict() or {}
        for ts_key in ["synced_at", "created_at", "updated_at", "last_comment_update_time"]:
            if ts_key in data and hasattr(data[ts_key], "isoformat"):
                data[ts_key] = data[ts_key].isoformat()
        results.append(data)

    return results


# ============================================================================
# Scheduled Closed Issues Sync & Task Cleanup (Option A)
# ============================================================================

def sync_closed_issues_for_user(
    user: User,
    db: firestore.Client
) -> Dict[str, Any]:
    """
    Queries GitHub for issues closed since the last sync, updates their state in Firestore,
    and deletes the corresponding Task document from users/{uid}/tasks.
    """
    if not user.github_access_token:
        return {"uid": user.uid, "closed_issues_count": 0, "status": "no_token"}

    headers = {
        "Authorization": f"Bearer {user.github_access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    since = user.last_assigned_issue_update_time
    closed_items: List[Dict[str, Any]] = []

    # 1. Query user-level closed issue filters
    filters = ["assigned", "mentioned", "created"]
    for filter_name in filters:
        params: Dict[str, Any] = {
            "filter": filter_name,
            "state": "closed",
            "per_page": 100,
        }
        if since:
            params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/issues"
        items, _ = fetch_single_issue_page(url=url, headers=headers, params=params)
        closed_items.extend(items)

    # 2. Query monitored repositories for closed issues
    for repo_path in user.monitored_repos:
        repo_clean = repo_path.strip().strip("/")
        if not repo_clean or "/" not in repo_clean:
            continue

        owner_part, repo_part = repo_clean.split("/", 1)
        params = {
            "state": "closed",
            "per_page": 100,
        }
        if since:
            params["since"] = since

        url = f"{GITHUB_API_BASE_URL}/repos/{owner_part}/{repo_part}/issues"
        items, _ = fetch_single_issue_page(url=url, headers=headers, params=params)
        for it in items:
            if "owner_part" not in it:
                it["_owner_fallback"] = owner_part
                it["_repo_fallback"] = repo_part
            closed_items.append(it)

    # Process and remove closed issues from tasks
    closed_count = 0
    issues_col = db.collection("users").document(user.uid).collection("issues")
    tasks_col = db.collection("users").document(user.uid).collection("tasks")

    seen_doc_ids: Set[str] = set()

    for item in closed_items:
        owner, repo = _extract_owner_and_repo(item)
        if owner == "unknown" and "_owner_fallback" in item:
            owner = item["_owner_fallback"]
        if repo == "unknown" and "_repo_fallback" in item:
            repo = item["_repo_fallback"]

        issue_number = item.get("number", 0)
        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", owner)
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)
        doc_id = f"{clean_owner}_{clean_repo}_{issue_number}"

        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)

        # Update issue in Firestore to state="closed"
        issue_ref = issues_col.document(doc_id)
        doc_snap = issue_ref.get()
        if doc_snap.exists:
            issue_ref.set({
                "state": "closed",
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)

        # Delete corresponding task if it exists
        task_doc_id = f"task_{doc_id}"
        task_ref = tasks_col.document(task_doc_id)
        task_snap = task_ref.get()
        if task_snap.exists:
            task_ref.delete()
            closed_count += 1
            logger.info(f"Deleted task {task_doc_id} for UID {user.uid} because GitHub issue was closed.")

    # Update sync timestamp
    now_iso = datetime.now(timezone.utc).isoformat()
    user.last_assigned_issue_update_time = now_iso
    db.collection("users").document(user.uid).set({
        "last_assigned_issue_update_time": now_iso,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)

    logger.info(f"Closed issue sync completed for UID {user.uid}: {closed_count} tasks removed.")
    return {
        "uid": user.uid,
        "closed_issues_count": closed_count,
        "sync_time": now_iso,
        "status": "success"
    }


def sync_all_users_closed_issues(db: firestore.Client) -> Dict[str, Any]:
    """
    Sweeps through all users in Firestore and removes closed issues from their task list.
    """
    users_col = db.collection("users")
    users_docs = users_col.stream()
    total_closed_tasks_removed = 0
    users_processed = 0

    for doc_snap in users_docs:
        data = doc_snap.to_dict() or {}
        user = User.model_validate({**data, "uid": doc_snap.id})
        if user.github_access_token:
            try:
                res = sync_closed_issues_for_user(user=user, db=db)
                total_closed_tasks_removed += res.get("closed_issues_count", 0)
                users_processed += 1
            except Exception as e:
                logger.error(f"Error syncing closed issues for UID {user.uid}: {e}")

    logger.info(f"Total {total_closed_tasks_removed} closed tasks removed across {users_processed} users.")
    return {
        "users_processed": users_processed,
        "total_closed_tasks_removed": total_closed_tasks_removed,
    }