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

from firebase_functions import https_fn, tasks_fn, scheduler_fn, options
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

# Initialize Firebase Admin App if not already initialized
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()


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

