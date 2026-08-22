"""
Firebase Functions (2nd Gen) Python Backend
Associates and manages user-specific information for users authenticated via
Firebase Authentication (Google and GitHub providers supported).
Supports:
1. Asynchronous chained GitHub issue & comment pagination via Task Queue Functions
2. Task prioritization and asynchronous ranking lifecycle via Task Queue Functions
"""

from typing import Any, Dict, Optional
import datetime
import json
import logging
import re

from firebase_functions import https_fn, tasks_fn, scheduler_fn, firestore_fn, options
import firebase_admin
from firebase_admin import credentials, firestore, auth

from auth_utils import extract_provider_info, fetch_full_user_auth_record, verify_bearer_token
from user import User
from github import (
    start_user_github_sync,
    fetch_single_issue_page,
    process_and_save_issue_page,
    fetch_single_comment_page,
    process_and_save_comment_page,
    enqueue_issue_page_sync,
    enqueue_comment_page_sync,
    get_user_stored_issues,
    sync_all_users_closed_issues,
    Issue,
    GITHUB_API_BASE_URL
)
from task import (
    Task,
    update_needed_priorities,
    force_rerank_tasks,
    enqueue_task_ranking,
    get_user_tasks
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import os
import jinja2

# Jinja2 Environment for server-side HTML rendering
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
    autoescape=jinja2.select_autoescape(["html", "xml"])
)

# Initialize Firebase Admin App if not already initialized
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()


# ============================================================================
# Jinja2 Server-Rendered Main Page (Static Ranked Tasks)
# ============================================================================

@https_fn.on_request(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "options"])
)
def render_main_page(req: https_fn.Request) -> https_fn.Response:
    """
    Renders the static ranked tasks list for developer debugging using Jinja2 templates.
    Authenticates the user via __session cookie, Authorization header, or ?token= param.
    """
    if req.method == "OPTIONS":
        return https_fn.Response("", status=204)

    token_str = (
        req.cookies.get("__session")
        or req.args.get("token")
    )
    if not token_str and req.headers.get("Authorization"):
        auth_hdr = req.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token_str = auth_hdr.split("Bearer ", 1)[1].strip()

    decoded_token = None
    if token_str:
        try:
            decoded_token = auth.verify_id_token(token_str)
        except Exception as e:
            logger.debug(f"ID token verification failed or expired: {e}")

    template = jinja_env.get_template("main.html")

    if not decoded_token:
        html = template.render(
            is_authenticated=False,
            user=None,
            tasks=[]
        )
        return https_fn.Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})

    uid = decoded_token.get("uid")
    # Fetch user details
    user_doc = db.collection("users").document(uid).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    if not user_data:
        user_data = {
            "uid": uid,
            "email": decoded_token.get("email"),
            "display_name": decoded_token.get("name")
        }

    # Fetch tasks from users/{uid}/tasks
    tasks_col = db.collection("users").document(uid).collection("tasks")
    tasks_docs = tasks_col.stream()
    tasks_list = []
    for doc in tasks_docs:
        t_data = doc.to_dict() or {}
        tasks_list.append(t_data)

    # Sort descending from highest to lowest priority
    tasks_list.sort(key=lambda t: float(t.get("priority") or 0.0), reverse=True)

    html = template.render(
        is_authenticated=True,
        user=user_data,
        tasks=tasks_list
    )
    return https_fn.Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})


# ============================================================================
# Jinja2 Server-Rendered Settings Page (Simple CRUD)
# ============================================================================

@https_fn.on_request(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def render_settings_page(req: https_fn.Request) -> https_fn.Response:
    """
    Simple server-side CRUD settings page for configuring GitHub access token and monitored repos.
    - GET /settings: Renders settings form.
    - POST /settings: Updates User in Firestore and displays success message.
    """
    if req.method == "OPTIONS":
        return https_fn.Response("", status=204)

    token_str = (
        req.cookies.get("__session")
        or req.args.get("token")
    )
    if not token_str and req.headers.get("Authorization"):
        auth_hdr = req.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token_str = auth_hdr.split("Bearer ", 1)[1].strip()

    decoded_token = None
    if token_str:
        try:
            decoded_token = auth.verify_id_token(token_str)
        except Exception as e:
            logger.debug(f"Token verification failed: {e}")

    if not decoded_token:
        # Redirect unauthenticated users to / for login
        return https_fn.Response(
            "",
            status=302,
            headers={"Location": "/"}
        )

    uid = decoded_token.get("uid")
    user_ref = db.collection("users").document(uid)
    template = jinja_env.get_template("settings.html")

    if req.method == "POST":
        new_token = (req.form.get("github_access_token") or "").strip()
        raw_repos = (req.form.get("monitored_repos") or "").strip()
        repo_list = [r.strip() for r in raw_repos.split(",") if r.strip()]

        update_data: Dict[str, Any] = {
            "github_access_token": new_token if new_token else None,
            "monitored_repos": repo_list,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        user_ref.set(update_data, merge=True)

        doc_snap = user_ref.get()
        user_data = doc_snap.to_dict() if doc_snap.exists else {"uid": uid}

        html = template.render(
            user=user_data,
            saved=True
        )
        return https_fn.Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})

    # GET request: render form with current values
    doc_snap = user_ref.get()
    user_data = doc_snap.to_dict() if doc_snap.exists else {
        "uid": uid,
        "email": decoded_token.get("email"),
        "display_name": decoded_token.get("name")
    }

    html = template.render(
        user=user_data,
        saved=False
    )
    return https_fn.Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})


# ============================================================================
# User Profile Callable Functions
# ============================================================================

@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def associate_user_info(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Associates custom information with the authenticated user in Firestore using the User model.
    Accepts:
      - github_access_token (optional)
      - last_assigned_issue_update_time (optional)
      - monitored_repos (optional list of repository names, e.g. ["owner/repo"])
      - custom_data or associated_data (optional dictionary of arbitrary user properties)
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to associate information."
        )

    uid = req.auth.uid
    token = req.auth.token
    provider_info = extract_provider_info(token)

    # Payload provided by caller
    payload: Dict[str, Any] = req.data if isinstance(req.data, dict) else {}

    # Extract user-specific fields
    github_access_token = payload.get("github_access_token")
    last_assigned_issue_update_time = payload.get("last_assigned_issue_update_time")
    monitored_repos = payload.get("monitored_repos")
    custom_data = payload.get("custom_data") or payload.get("associated_data") or {}

    # Document reference in Firestore
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if doc_snap.exists:
        existing_data = doc_snap.to_dict() or {}
        user = User.from_dict(existing_data, uid=uid)

        # Update fields if provided
        if github_access_token is not None:
            user.github_access_token = github_access_token
        if last_assigned_issue_update_time is not None:
            user.last_assigned_issue_update_time = last_assigned_issue_update_time
        if monitored_repos is not None:
            user.monitored_repos = monitored_repos
        if custom_data:
            user.custom_data.update(custom_data)

        # Ensure authentication and provider fields stay synced
        user.email = token.get("email") or user.email
        user.email_verified = token.get("email_verified", user.email_verified)
        user.display_name = token.get("name") or user.display_name
        user.photo_url = token.get("picture") or user.photo_url
        user.primary_provider = provider_info.get("primary_provider") or user.primary_provider
        user.google_id = provider_info.get("google_id") or user.google_id
        user.github_id = provider_info.get("github_id") or user.github_id
        user.linked_providers = provider_info.get("linked_providers") or user.linked_providers

        action = "updated"
    else:
        user = User.from_auth_token(
            token_dict=token,
            provider_info=provider_info,
            github_access_token=github_access_token,
            last_assigned_issue_update_time=last_assigned_issue_update_time,
            monitored_repos=monitored_repos,
            custom_data=custom_data,
        )
        action = "created"

    user_ref.set(user.to_dict(for_firestore=True), merge=True)
    logger.info(f"User {action} in Firestore for UID {uid} (provider: {provider_info.get('primary_provider_name')})")

    return {
        "status": "success",
        "action": action,
        "uid": uid,
        "provider": provider_info.get("primary_provider_name"),
        "user": user.to_dict(for_firestore=False),
    }


@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def get_user_info(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Retrieves the authenticated user's User document from Firestore.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to retrieve information."
        )

    uid = req.auth.uid
    token = req.auth.token
    provider_info = extract_provider_info(token)

    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if not doc_snap.exists:
        user = User.from_auth_token(
            token_dict=token,
            provider_info=provider_info,
        )
        user_ref.set(user.to_dict(for_firestore=True))
    else:
        user = User.from_dict(doc_snap.to_dict() or {}, uid=uid)

    return {
        "status": "success",
        "user": user.to_dict(for_firestore=False),
        "auth_provider": provider_info,
    }


@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def sync_auth_profile(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Synchronizes Firebase Auth profile data into Firestore.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to sync profile."
        )

    uid = req.auth.uid
    full_auth_record = fetch_full_user_auth_record(uid)
    provider_info = extract_provider_info(req.auth.token)

    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if doc_snap.exists:
        user = User.from_dict(doc_snap.to_dict() or {}, uid=uid)
    else:
        user = User(uid=uid)

    user.email = full_auth_record.get("email")
    user.email_verified = full_auth_record.get("email_verified", False)
    user.display_name = full_auth_record.get("display_name")
    user.photo_url = full_auth_record.get("photo_url")
    user.primary_provider = provider_info.get("primary_provider")
    user.google_id = provider_info.get("google_id")
    user.github_id = provider_info.get("github_id")
    user.linked_providers = provider_info.get("linked_providers", [])

    user_ref.set(user.to_dict(for_firestore=True), merge=True)
    logger.info(f"Synchronized User auth profile for UID {uid}")

    return {
        "status": "success",
        "message": "Auth profile synced successfully.",
        "user": user.to_dict(for_firestore=False),
        "auth_record": full_auth_record,
    }


@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def delete_user_info(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Deletes the user's User document from Firestore.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to delete associated data."
        )

    uid = req.auth.uid
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if doc_snap.exists:
        user_ref.delete()
        logger.info(f"Deleted User document for UID {uid}")
        return {"status": "success", "message": f"User document for UID {uid} has been deleted."}
    else:
        return {"status": "not_found", "message": "No User document found for this user."}


# ============================================================================
# GitHub Issues Trigger & Retrieval
# ============================================================================

@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def sync_github_issues(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Kicks off asynchronous GitHub issue synchronization in the background.
    Dispatches initial pagination tasks for assigned, mentioned, created, and monitored repo issues.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to sync GitHub issues."
        )

    uid = req.auth.uid
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if not doc_snap.exists:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.NOT_FOUND,
            message="User document not found in Firestore. Please configure your GitHub access token first."
        )

    user = User.from_dict(doc_snap.to_dict() or {}, uid=uid)

    if not user.github_access_token:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            message="User does not have a github_access_token configured."
        )

    payload = req.data if isinstance(req.data, dict) else {}
    state = payload.get("state", "open")

    try:
        result = start_user_github_sync(user=user, db=db, state=state)
        return result
    except Exception as e:
        logger.error(f"Error starting GitHub sync for UID {uid}: {e}")
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message=f"Failed to start GitHub sync: {str(e)}"
        )


@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def get_stored_issues(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Retrieves stored issues for the authenticated user from Firestore under users/{uid}/issues.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to retrieve issues."
        )

    uid = req.auth.uid
    payload = req.data if isinstance(req.data, dict) else {}
    limit = int(payload.get("limit", 100))

    issues = get_user_stored_issues(uid=uid, db=db, limit=limit)
    return {
        "status": "success",
        "count": len(issues),
        "issues": issues,
    }


# ============================================================================
# Firebase Task Queue Functions: Chained Issue & Comment Pagination
# ============================================================================

@tasks_fn.on_task_dispatched(
    retry_config=options.RetryConfig(
        max_attempts=3,
        min_backoff_seconds=10,
        max_backoff_seconds=300,
        max_doublings=3
    ),
    rate_limits=options.RateLimits(
        max_concurrent_dispatches=10,
        max_dispatches_per_second=10
    )
)
def sync_github_issues_page(req: tasks_fn.CallableRequest) -> Dict[str, Any]:
    """
    Processes one page of GitHub issues for a given user, stores them in Firestore,
    enqueues comment fetching for each issue, and chains to the next page if available.
    """
    data = req.data if isinstance(req.data, dict) else {}
    uid = data.get("uid")
    url = data.get("url")
    params = data.get("params") or {}
    reason = data.get("reason", "assigned")
    owner_fallback = data.get("owner_fallback")
    repo_fallback = data.get("repo_fallback")

    if not uid or not url:
        raise tasks_fn.HttpsError(
            code=tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="Missing 'uid' or 'url' in task data."
        )

    from github import execute_issue_page_sync
    return execute_issue_page_sync(
        uid=uid,
        url=url,
        params=params if params else None,
        reason=reason,
        owner_fallback=owner_fallback,
        repo_fallback=repo_fallback,
        db=db
    )


@tasks_fn.on_task_dispatched(
    retry_config=options.RetryConfig(
        max_attempts=3,
        min_backoff_seconds=10,
        max_backoff_seconds=300,
        max_doublings=3
    ),
    rate_limits=options.RateLimits(
        max_concurrent_dispatches=15,
        max_dispatches_per_second=15
    )
)
def sync_issue_comments_page(req: tasks_fn.CallableRequest) -> Dict[str, Any]:
    """
    Processes one page of comments for an issue, updates Firestore,
    and chains to the next page of comments if available.
    """
    data = req.data if isinstance(req.data, dict) else {}
    uid = data.get("uid")
    issue_doc_id = data.get("issue_doc_id")
    comments_url = data.get("comments_url")
    params = data.get("params") or {}

    if not uid or not issue_doc_id or not comments_url:
        raise tasks_fn.HttpsError(
            code=tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="Missing required parameters for comment sync."
        )

    from github import execute_comment_page_sync
    return execute_comment_page_sync(
        uid=uid,
        issue_doc_id=issue_doc_id,
        comments_url=comments_url,
        params=params if params else None,
        db=db
    )


# ============================================================================
# Firebase Task Queue Functions: Task Ranking
# ============================================================================

@tasks_fn.on_task_dispatched(
    retry_config=options.RetryConfig(
        max_attempts=3,
        min_backoff_seconds=10,
        max_backoff_seconds=300,
        max_doublings=3
    ),
    rate_limits=options.RateLimits(
        max_concurrent_dispatches=5,
        max_dispatches_per_second=10
    )
)
def rank_user_tasks(req: tasks_fn.CallableRequest) -> Dict[str, Any]:
    """
    Firebase Task Queue function for asynchronous ranking.
    Dispatched via Cloud Tasks when task priorities need update.
    """
    data = req.data if isinstance(req.data, dict) else {}
    uid = data.get("uid")
    if not uid:
        raise tasks_fn.HttpsError(
            code=tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT,
            message="Missing 'uid' in task payload."
        )

    logger.info(f"Executing asynchronous ranking for UID {uid} in Task Queue worker.")
    result = update_needed_priorities(uid=uid, db=db)
    return result


# ============================================================================
# Task & Priority Ranking Callable Functions
# ============================================================================

@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def update_task_priorities(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Enqueues an asynchronous task in the Firebase Task Queue to update priorities.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to update task priorities."
        )

    uid = req.auth.uid
    try:
        enqueue_res = enqueue_task_ranking(uid=uid, db=db)
        return {
            "status": "enqueued",
            "message": "Task priority ranking enqueued asynchronously.",
            "uid": uid,
            "enqueue_info": enqueue_res,
        }
    except Exception as e:
        logger.error(f"Error enqueuing task priorities update for UID {uid}: {e}")
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message=f"Failed to enqueue task priority update: {str(e)}"
        )


@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def force_rerank_all_tasks(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Forces all tasks for the authenticated user to be reranked:
    Sets priority_needs_updated = True on all tasks and enqueues the ranker.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to force rerank tasks."
        )

    uid = req.auth.uid
    try:
        result = force_rerank_tasks(uid=uid, db=db)
        return result
    except Exception as e:
        logger.error(f"Error forcing task rerank for UID {uid}: {e}")
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL,
            message=f"Failed to force rerank tasks: {str(e)}"
        )


@https_fn.on_call(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"])
)
def get_user_task_list(req: https_fn.CallableRequest) -> Dict[str, Any]:
    """
    Retrieves all tasks for the authenticated user from Firestore under users/{uid}/tasks.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to retrieve tasks."
        )

    uid = req.auth.uid
    payload = req.data if isinstance(req.data, dict) else {}
    limit = int(payload.get("limit", 100))

    tasks = get_user_tasks(uid=uid, db=db, limit=limit)
    return {
        "status": "success",
        "count": len(tasks),
        "tasks": tasks,
    }


# ============================================================================
# HTTP REST API Function
# ============================================================================

@https_fn.on_request(
    cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "delete", "options"])
)
def user_api(req: https_fn.Request) -> https_fn.Response:
    """
    RESTful endpoint:
    - GET /: Returns User data.
    - POST /: Associates/updates User information.
    - DELETE /: Deletes User document.
    Requires header: 'Authorization: Bearer <ID_TOKEN>'.
    """
    if req.method == "OPTIONS":
        return https_fn.Response("", status=204)

    auth_header = req.headers.get("Authorization")
    try:
        decoded_token = verify_bearer_token(auth_header)
    except https_fn.HttpsError as e:
        return https_fn.Response(
            json.dumps({"error": e.message}),
            status=401,
            headers={"Content-Type": "application/json"}
        )

    uid = decoded_token.get("uid")
    provider_info = extract_provider_info(decoded_token)
    user_ref = db.collection("users").document(uid)

    if req.method == "GET":
        doc_snap = user_ref.get()
        if doc_snap.exists:
            user = User.from_dict(doc_snap.to_dict() or {}, uid=uid)
        else:
            user = User.from_auth_token(decoded_token, provider_info)

        return https_fn.Response(
            json.dumps({
                "status": "success",
                "uid": uid,
                "provider": provider_info.get("primary_provider_name"),
                "user": user.to_dict(for_firestore=False)
            }, default=str),
            status=200,
            headers={"Content-Type": "application/json"}
        )

    elif req.method == "POST":
        try:
            body = req.get_json(silent=True) or {}
        except Exception:
            body = {}

        doc_snap = user_ref.get()
        if doc_snap.exists:
            user = User.from_dict(doc_snap.to_dict() or {}, uid=uid)
        else:
            user = User.from_auth_token(decoded_token, provider_info)

        if "github_access_token" in body:
            user.github_access_token = body.get("github_access_token")
        if "last_assigned_issue_update_time" in body:
            user.last_assigned_issue_update_time = body.get("last_assigned_issue_update_time")
        if "monitored_repos" in body:
            user.monitored_repos = body.get("monitored_repos")
        if "custom_data" in body:
            user.custom_data.update(body.get("custom_data") or {})
        elif "associated_data" in body:
            user.custom_data.update(body.get("associated_data") or {})

        user_ref.set(user.to_dict(for_firestore=True), merge=True)
        return https_fn.Response(
            json.dumps({
                "status": "success",
                "message": "User data updated successfully.",
                "user": user.to_dict(for_firestore=False)
            }),
            status=200,
            headers={"Content-Type": "application/json"}
        )

    elif req.method == "DELETE":
        user_ref.delete()
        return https_fn.Response(
            json.dumps({
                "status": "success",
                "message": f"User document for UID {uid} deleted."
            }),
            status=200,
            headers={"Content-Type": "application/json"}
        )

    return https_fn.Response(
        json.dumps({"error": f"Method {req.method} not allowed."}),
        status=405,
        headers={"Content-Type": "application/json"}
    )


# ============================================================================
# Scheduled Functions: Closed Issues Sync & Task Cleanup (Option A)
# ============================================================================

@scheduler_fn.on_schedule(
    schedule="every 5 minutes",
    retry_count=1
)
def scheduled_sync_closed_issues(event: scheduler_fn.ScheduledEvent) -> None:
    """
    Scheduled function that runs every 5 minutes to detect closed GitHub issues
    and remove them from users' task lists in Firestore.
    """
    logger.info("Starting scheduled 5-minute sync for closed GitHub issues.")
    res = sync_all_users_closed_issues(db=db)
    logger.info(f"Completed scheduled sync for closed issues: {res}")


# ============================================================================
# Cloud Firestore Triggers: Auto-Sync on User Settings Creation or Change
# ============================================================================

@firestore_fn.on_document_written(document="users/{uid}")
def on_user_settings_changed(
    event: firestore_fn.Event[firestore_fn.Change[firestore_fn.DocumentSnapshot | None]]
) -> None:
    """
    Cloud Firestore trigger that invokes start_user_github_sync if the user's
    settings (github_access_token or monitored_repos) are newly created or changed.
    """
    if event.data is None or event.data.after is None:
        logger.info("User document was deleted; skipping GitHub sync.")
        return

    after_snap = event.data.after
    before_snap = event.data.before

    # If document does not exist after write, do nothing
    if hasattr(after_snap, "exists") and not after_snap.exists:
        logger.info("User document does not exist after write; skipping.")
        return

    after_data = after_snap.to_dict() or {}
    before_data = {}
    is_new = (before_snap is None) or (hasattr(before_snap, "exists") and not before_snap.exists)
    if not is_new and before_snap is not None:
        before_data = before_snap.to_dict() or {}

    after_token = after_data.get("github_access_token")
    before_token = before_data.get("github_access_token")

    after_repos = sorted(after_data.get("monitored_repos") or [])
    before_repos = sorted(before_data.get("monitored_repos") or [])

    if not after_token:
        logger.info(f"User {event.params.get('uid')} has no github_access_token configured; skipping sync.")
        return

    token_changed = after_token != before_token
    repos_changed = after_repos != before_repos

    if is_new or token_changed or repos_changed:
        uid = event.params.get("uid")
        logger.info(
            f"User settings created/changed for UID {uid} "
            f"(is_new={is_new}, token_changed={token_changed}, repos_changed={repos_changed}). "
            f"Triggering background GitHub sync."
        )
        user = User.from_dict(after_data, uid=uid)
        start_user_github_sync(user=user, db=db, state="open")



