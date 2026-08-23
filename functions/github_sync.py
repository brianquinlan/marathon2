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
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum

from firebase_admin import functions as admin_functions
from github import Auth, Github, GithubException, GithubObject
from github.Issue import Issue as PyghIssue
from github.IssueComment import IssueComment as PyghComment
from github.PaginatedList import PaginatedList
from google.cloud import firestore
from pydantic import BaseModel, ConfigDict, Field

from task import ensure_task_for_issue
from user import User

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
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_api_dict(cls, data: dict[str, object]) -> "Comment":
        raw_user = data.get("user")
        user_login = "unknown"
        if isinstance(raw_user, dict):
            user_login = str(raw_user.get("login", "unknown"))
        raw_id = data.get("id", 0)
        c_id = int(raw_id) if isinstance(raw_id, int) or (isinstance(raw_id, str) and raw_id.isdigit()) else 0
        return cls(
            id=c_id,
            user_login=user_login,
            body=str(data.get("body") or ""),
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
    body: str | None = None
    user_login: str
    assignee_logins: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    state: str = "open"
    last_comment_update_time: datetime | None = None
    comments: list[Comment] = Field(default_factory=list)
    association_reasons: list[AssociationReason] = Field(default_factory=list)
    title: str | None = None

    @property
    def doc_id(self) -> str:
        """Unique document ID for Firestore: {owner}_{repo}_{number}"""
        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", self.owner)
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", self.repo)
        return f"{clean_owner}_{clean_repo}_{self.issue_number}"

    @property
    def created_atr(self) -> datetime:
        return self.created_at


def _safe_int(val: object, default: int = 0) -> int:
    """Safely converts an object to int or returns default."""
    if isinstance(val, int):
        return val
    if isinstance(val, str) and (val.isdigit() or (val.startswith("-") and val[1:].isdigit())):
        return int(val)
    return default


def _ensure_utc(dt_val: datetime | None) -> datetime | None:
    if dt_val is None:
        return None
    return dt_val if dt_val.tzinfo else dt_val.replace(tzinfo=timezone.utc)


def _parse_github_datetime(dt_val: datetime | str | object | None) -> datetime | None:
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
# PyGithub Client & Adaptors
# ============================================================================


def get_github_client(access_token: str) -> Github:
    """
    Creates an authenticated PyGithub client instance.
    """
    auth = Auth.Token(access_token)
    return Github(auth=auth, timeout=20, user_agent="Firebase-GitHub-Sync-App")


def comment_from_pygithub(pygh_comment: PyghComment) -> Comment:
    """
    Converts a PyGithub IssueComment object to our internal Comment model.
    """
    author = "unknown"
    if pygh_comment.user:
        author = pygh_comment.user.login or "unknown"
    return Comment(
        id=pygh_comment.id,
        user_login=author,
        body=pygh_comment.body or "",
        created_at=_ensure_utc(pygh_comment.created_at),
        updated_at=_ensure_utc(pygh_comment.updated_at),
    )


def issue_from_pygithub(
    pygh_issue: PyghIssue,
    reason: AssociationReason | str,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
) -> Issue:
    """
    Converts a PyGithub Issue object to our internal Issue model.
    """
    owner = owner_fallback or "unknown"
    repo = repo_fallback or "unknown"
    if pygh_issue.repository:
        if pygh_issue.repository.owner:
            owner = pygh_issue.repository.owner.login or owner
        repo = pygh_issue.repository.name or repo

    is_pr = pygh_issue.pull_request is not None
    issue_type = IssueType.PULL_REQUEST if is_pr else IssueType.ISSUE
    author = pygh_issue.user.login if pygh_issue.user else "unknown"
    assignees: list[str] = []
    if pygh_issue.assignees:
        assignees = [str(a.login) for a in pygh_issue.assignees if a and a.login]

    parsed_reason = (
        AssociationReason(reason)
        if isinstance(reason, str) and reason in AssociationReason._value2member_map_
        else (reason if isinstance(reason, AssociationReason) else AssociationReason.ASSIGNED)
    )

    comments_url = (
        pygh_issue.comments_url or f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/issues/{pygh_issue.number}/comments"
    )

    return Issue(
        url=str(pygh_issue.html_url or f"https://github.com/{owner}/{repo}/issues/{pygh_issue.number}"),
        owner=owner,
        repo=repo,
        issue_number=pygh_issue.number,
        number=pygh_issue.number,
        title=pygh_issue.title,
        issue_type=issue_type,
        state=pygh_issue.state or "open",
        comments_url=str(comments_url),
        body=pygh_issue.body,
        user_login=author,
        assignee_logins=assignees,
        created_at=_ensure_utc(pygh_issue.created_at) or datetime.now(timezone.utc),
        updated_at=_ensure_utc(pygh_issue.updated_at) or datetime.now(timezone.utc),
        association_reasons=[parsed_reason],
    )


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


def fetch_single_comment_page_pygithub(
    client: Github,
    owner: str,
    repo: str,
    issue_number: int,
    since: datetime | None = None,
    page: int = 0,
    per_page: int = 100,
) -> tuple[list[Comment], bool]:
    """
    Fetches a single page (0-indexed) of comments for an issue using PyGithub.
    Returns (comments, has_next_page).
    """
    try:
        repository = client.get_repo(f"{owner}/{repo}")
        issue = repository.get_issue(issue_number)
        paginated: PaginatedList[PyghComment] = issue.get_comments(since=since or GithubObject.NotSet)
        pygh_comments = list(paginated.get_page(page))
        comments = [comment_from_pygithub(c) for c in pygh_comments]
        has_next = len(pygh_comments) >= per_page
        return comments, has_next
    except GithubException as e:
        if e.status in (401, 403):
            raise PermissionError(f"GitHub API authorization error ({e.status}): {e.data}") from e
        elif e.status == 404:
            return [], False
        raise
    except Exception as e:
        logger.error(f"Error fetching comments for {owner}/{repo}#{issue_number} (page={page}): {e}")
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


def fetch_single_comment_page(
    comments_url: str, headers: dict[str, str], params: Mapping[str, object] | None = None, client: Github | None = None
) -> tuple[list[Comment], str | None]:
    """
    Fetches a single page of comments and returns (comments, next_page_url).
    Delegates to PyGithub client when available.
    """
    try:
        token = headers.get("Authorization", "").replace("Bearer ", "").replace("token ", "").strip()
        g = client or (get_github_client(token) if token else None)
        if not g:
            return [], None

        match = re.search(r"repos/([^/]+)/([^/]+)/issues/(\d+)/comments", comments_url)
        if not match:
            return [], None

        owner, repo, issue_num = match.group(1), match.group(2), int(match.group(3))
        page = _safe_int(params.get("page"), default=0) if params else 0
        per_page = _safe_int(params.get("per_page"), default=100) if params else 100
        since_raw = params.get("since") if params else None
        since_dt = _parse_github_datetime(since_raw)

        comments, has_next = fetch_single_comment_page_pygithub(
            client=g, owner=owner, repo=repo, issue_number=issue_num, since=since_dt, page=page, per_page=per_page
        )
        next_url = f"{comments_url}?page={page + 1}" if has_next else None
        return comments, next_url
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"Error fetching comments from {comments_url}: {e}")
        return [], None


# ============================================================================
# Page Processing & Firestore Persistence
# ============================================================================


def process_and_save_issue_page(
    uid: str,
    raw_items: list[dict[str, object]],
    reason: AssociationReason | str,
    db: firestore.Client,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
) -> list[str]:
    """
    Parses a page of raw GitHub issue dicts and merges them with existing Firestore documents.
    Note: Tasks are only created here if the issue has 0 comments (otherwise created when comments finish).
    """
    if not raw_items:
        return []

    issues_col = db.collection("users").document(uid).collection("issues")
    saved_doc_ids: list[str] = []

    for item in raw_items:
        owner, repo = _extract_owner_and_repo(item)
        if owner == "unknown" and owner_fallback:
            owner = owner_fallback
        if repo == "unknown" and repo_fallback:
            repo = repo_fallback

        raw_num = item.get("number", 0)
        issue_number = int(raw_num) if isinstance(raw_num, (int, str)) and str(raw_num).isdigit() else 0
        is_pr = bool(item.get("pull_request"))
        issue_type = IssueType.PULL_REQUEST if is_pr else IssueType.ISSUE

        comments_url = str(
            item.get("comments_url") or f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        )
        created_at = _parse_github_datetime(item.get("created_at")) or datetime.now(timezone.utc)
        updated_at = _parse_github_datetime(item.get("updated_at")) or datetime.now(timezone.utc)

        raw_assignees = item.get("assignees", [])
        assignees: list[str] = []
        if isinstance(raw_assignees, list):
            for a in raw_assignees:
                if isinstance(a, dict) and a.get("login"):
                    assignees.append(str(a["login"]))

        raw_single_a = item.get("assignee")
        if not assignees and isinstance(raw_single_a, dict):
            single_a = raw_single_a.get("login")
            if single_a:
                assignees = [str(single_a)]

        clean_owner = re.sub(r"[^a-zA-Z0-9_-]", "_", owner)
        clean_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)
        doc_id = f"{clean_owner}_{clean_repo}_{issue_number}"

        # Fetch existing document to merge association_reasons and preserve existing comments
        doc_ref = issues_col.document(doc_id)
        doc_snap = doc_ref.get()

        parsed_reason = (
            AssociationReason(reason)
            if isinstance(reason, str) and reason in AssociationReason._value2member_map_
            else (reason if isinstance(reason, AssociationReason) else AssociationReason.ASSIGNED)
        )
        reasons: set[AssociationReason] = {parsed_reason}
        existing_comments: list[Comment] = []
        last_comment_time: datetime | None = None

        if doc_snap.exists:
            existing_data = doc_snap.to_dict() or {}
            for r in existing_data.get("association_reasons") or []:
                if isinstance(r, str) and r in AssociationReason._value2member_map_:
                    reasons.add(AssociationReason(r))
                elif isinstance(r, AssociationReason):
                    reasons.add(r)
            existing_comments = [
                Comment.model_validate(c) for c in existing_data.get("comments", []) if isinstance(c, dict)
            ]
            last_comment_time = _parse_github_datetime(existing_data.get("last_comment_update_time"))

        raw_user = item.get("user")
        user_login_str = str(raw_user.get("login", "unknown")) if isinstance(raw_user, dict) else "unknown"

        issue = Issue(
            url=str(item.get("html_url") or item.get("url") or ""),
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            number=issue_number,
            title=str(item["title"]) if item.get("title") is not None else None,
            issue_type=issue_type,
            state=str(item.get("state", "open")),
            comments_url=comments_url,
            body=str(item["body"]) if item.get("body") is not None else None,
            user_login=user_login_str,
            assignee_logins=assignees,
            created_at=created_at,
            updated_at=updated_at,
            last_comment_update_time=last_comment_time,
            comments=existing_comments,
            association_reasons=sorted(list(reasons), key=lambda x: x.value),
        )

        issue_dict = issue.model_dump(mode="json")
        issue_dict["synced_at"] = firestore.SERVER_TIMESTAMP
        doc_ref.set(issue_dict, merge=True)

        # Only create Task immediately if the issue has 0 comments (already fully imported)
        raw_cc = item.get("comments", 0)
        comments_count = int(raw_cc) if isinstance(raw_cc, (int, str)) and str(raw_cc).isdigit() else 0
        if comments_count == 0:
            ensure_task_for_issue(uid=uid, issue_id=doc_id, issue_data=issue.model_dump(mode="json"), db=db)
            logger.info(f"Created/updated Task for issue {doc_id} with 0 comments.")

        saved_doc_ids.append(doc_id)

    logger.info(f"Processed and saved {len(saved_doc_ids)} issues for UID {uid} under reason '{reason}'.")
    return saved_doc_ids


def process_and_save_comment_page(
    uid: str, issue_doc_id: str, new_comments: list[Comment], db: firestore.Client, is_last_page: bool = False
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
    existing_comments = [Comment.model_validate(c) for c in existing_data.get("comments", []) if isinstance(c, dict)]

    # Deduplicate comments by ID
    comments_by_id: dict[int, Comment] = {c.id: c for c in existing_comments}
    for nc in new_comments:
        comments_by_id[nc.id] = nc

    all_comments = sorted(
        list(comments_by_id.values()), key=lambda c: c.created_at or datetime.min.replace(tzinfo=timezone.utc)
    )

    valid_times: list[datetime] = [t for t in [c.updated_at or c.created_at for c in all_comments] if t is not None]
    last_comment_time: datetime | None = max(valid_times, default=None)

    update_payload: dict[str, object] = {
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
        ensure_task_for_issue(uid=uid, issue_id=issue_doc_id, issue_data=updated_issue_data, db=db)
        logger.info(f"Issue {issue_doc_id} and all comments fully imported. Created/updated Task.")

    return len(new_comments)


# ============================================================================
# Execution & Task Queue Enqueuers (Chaining Pagination)
# ============================================================================


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
    Executes fetching one page of issues and scheduling comment sync for each.
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

    # Enqueue comment fetching tasks for each issue in this page
    for item in items:
        raw_num = item.get("number", 0)
        issue_number = int(raw_num) if isinstance(raw_num, (int, str)) and str(raw_num).isdigit() else 0
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

        raw_cc = item.get("comments", 0)
        comments_count = int(raw_cc) if isinstance(raw_cc, (int, str)) and str(raw_cc).isdigit() else 0
        if comments_count > 0:
            comment_params: dict[str, object] = {"per_page": 100, "page": 0}
            if user.last_assigned_issue_update_time:
                comment_params["since"] = user.last_assigned_issue_update_time
            enqueue_comment_page_sync(
                uid=uid, issue_doc_id=doc_id, comments_url=str(comments_url), params=comment_params, db=db
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


def execute_comment_page_sync(
    uid: str, issue_doc_id: str, comments_url: str, params: Mapping[str, object] | None, db: firestore.Client
) -> dict[str, object]:
    """
    Executes fetching one page of comments and chaining if necessary.
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
    comments, next_url = fetch_single_comment_page(
        comments_url=comments_url, headers=headers, params=params, client=client
    )

    saved_count = process_and_save_comment_page(
        uid=uid, issue_doc_id=issue_doc_id, new_comments=comments, db=db, is_last_page=(next_url is None)
    )

    next_task_id = None
    if next_url:
        page_val = _safe_int(params.get("page"), default=0) if params else 0
        next_params: dict[str, object] = dict(params) if params else {}
        next_params["page"] = page_val + 1

        next_task_id = enqueue_comment_page_sync(
            uid=uid, issue_doc_id=issue_doc_id, comments_url=comments_url, params=next_params, db=db
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
    params: Mapping[str, object] | None = None,
    reason: AssociationReason | str = AssociationReason.ASSIGNED,
    owner_fallback: str | None = None,
    repo_fallback: str | None = None,
    db: firestore.Client | None = None,
) -> str | None:
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
            "params": dict(params) if params else {},
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
                        db=db,
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
    params: Mapping[str, object] | None = None,
    db: firestore.Client | None = None,
) -> str | None:
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
            "params": dict(params) if params else {"per_page": 100},
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
                        uid=uid, issue_doc_id=issue_doc_id, comments_url=comments_url, params=params, db=db
                    )
                except Exception as ex:
                    logger.error(f"Error in fallback execute_comment_page_sync: {ex}")

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
        return "fallback_dispatched"


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


def get_user_stored_issues(uid: str, db: firestore.Client, limit: int = 100) -> list[dict[str, object]]:
    """
    Retrieves stored issues from Firestore for a given user UID.
    """
    issues_ref = db.collection("users").document(uid).collection("issues")
    docs = issues_ref.limit(limit).stream()

    results: list[dict[str, object]] = []
    for doc_snap in docs:
        data = doc_snap.to_dict() or {}
        for ts_key in ["synced_at", "created_at", "updated_at", "last_comment_update_time"]:
            ts_val = data.get(ts_key)
            if isinstance(ts_val, datetime):
                data[ts_key] = ts_val.isoformat()
        results.append(data)

    return results


# ============================================================================
# Scheduled Closed Issues Sync & Task Cleanup
# ============================================================================


def sync_closed_issues_for_user(user: User, db: firestore.Client, client: Github | None = None) -> dict[str, object]:
    """
    Queries GitHub for issues closed since the last sync via PyGithub, updates their state in Firestore,
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
    issues_col = db.collection("users").document(user_uid).collection("issues")
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

        # Update issue in Firestore to state="closed"
        issue_ref = issues_col.document(doc_id)
        doc_snap = issue_ref.get()
        if doc_snap.exists:
            issue_ref.set({"state": "closed", "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)

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
