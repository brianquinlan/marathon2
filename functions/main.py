"""
Firebase Functions (2nd Gen) Python Backend
Associates and manages user-specific information for users authenticated via
Firebase Authentication (Google and GitHub providers supported).
Supports:
1. Asynchronous chained GitHub issue & comment pagination via Task Queue Functions
2. Task prioritization and asynchronous ranking lifecycle via Task Queue Functions
"""

import json
import logging
import os

import firebase_admin
import jinja2
from firebase_admin import auth
from firebase_functions import firestore_fn, https_fn, options, scheduler_fn, tasks_fn
from flask import Response
from google.cloud import firestore
from google.cloud.firestore import SERVER_TIMESTAMP

from auth_utils import extract_provider_info, fetch_full_user_auth_record, verify_bearer_token
from github_sync import start_user_github_sync, sync_all_users_closed_issues
from task import enqueue_task_ranking, force_rerank_tasks, get_user_tasks, update_task_priority
from user import User

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Jinja2 Environment for server-side HTML rendering
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR), autoescape=jinja2.select_autoescape(["html", "xml"])
)

# Initialize Firebase Admin App if not already initialized
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db: firestore.Client = firestore.Client()


# ============================================================================
# Jinja2 Server-Rendered Main Page (Static Ranked Tasks)
# ============================================================================


@https_fn.on_request(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def render_main_page(req: https_fn.Request) -> Response:
    """
    Renders the static ranked tasks list for developer debugging using Jinja2 templates.
    Authenticates the user via __session cookie, Authorization header, or ?token= param.
    """
    if req.method == "OPTIONS":
        return Response("", status=204)

    token_str = req.cookies.get("__session") or req.args.get("token")
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
        html = template.render(is_authenticated=False, user=None, tasks=[])
        return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})

    uid = str(decoded_token.get("uid") or "")
    # Fetch user details
    user_doc = db.collection("users").document(uid).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    if not user_data:
        user_data = {"uid": uid, "email": decoded_token.get("email"), "display_name": decoded_token.get("name")}

    if req.method == "POST":
        force_rerank_tasks(uid=uid, db=db)

    # Fetch tasks from users/{uid}/tasks
    tasks_col = db.collection("users").document(uid).collection("tasks")
    tasks_docs = tasks_col.stream()
    tasks_list: list[dict[str, object]] = []
    unranked_task_ids: list[str] = []
    for doc in tasks_docs:
        t_data = doc.to_dict() or {}
        tasks_list.append(t_data)
        if t_data.get("priority_needs_updated"):
            raw_id = doc.id
            if raw_id:
                unranked_task_ids.append(str(raw_id))

    # Auto-dispatch ranking for any tasks that need ranking
    for unranked_id in unranked_task_ids:
        enqueue_task_ranking(uid=uid, task_id=unranked_id, db=db)

    # Sort descending from highest to lowest priority
    tasks_list.sort(key=lambda t: float(t.get("priority") or 0.0), reverse=True)  # type: ignore

    html = template.render(is_authenticated=True, user=user_data, tasks=tasks_list)
    return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})


# ============================================================================
# Jinja2 Server-Rendered Settings Page (Simple CRUD)
# ============================================================================


@https_fn.on_request(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def render_settings_page(req: https_fn.Request) -> Response:
    """
    Simple server-side CRUD settings page for configuring GitHub access token and monitored repos.
    - GET /settings: Renders settings form.
    - POST /settings: Updates User in Firestore and displays success message.
    """
    if req.method == "OPTIONS":
        return Response("", status=204)

    token_str = req.cookies.get("__session") or req.args.get("token")
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
        return Response("", status=302, headers={"Location": "/"})

    uid = str(decoded_token.get("uid") or "")
    user_ref = db.collection("users").document(uid)
    template = jinja_env.get_template("settings.html")

    if req.method == "POST":
        new_token = (req.form.get("github_access_token") or "").strip()
        new_gemini_key = (req.form.get("gemini_api_key") or "").strip()
        raw_repos = (req.form.get("monitored_repos") or "").strip()
        repo_list = [r.strip() for r in raw_repos.split(",") if r.strip()]

        update_data: dict[str, object] = {
            "github_access_token": new_token if new_token else None,
            "gemini_api_key": new_gemini_key if new_gemini_key else None,
            "monitored_repos": repo_list,
            "updated_at": SERVER_TIMESTAMP,
        }

        user_ref.set(update_data, merge=True)

        doc_snap = user_ref.get()
        user_data = doc_snap.to_dict() if doc_snap.exists else {"uid": uid}

        html = template.render(user=user_data, saved=True)
        return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})

    # GET request: render form with current values
    doc_snap = user_ref.get()
    user_data = (
        doc_snap.to_dict()
        if doc_snap.exists
        else {"uid": uid, "email": decoded_token.get("email"), "display_name": decoded_token.get("name")}
    )

    html = template.render(user=user_data, saved=False)
    return Response(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})


# ============================================================================
# User Profile Callable Functions
# ============================================================================


@https_fn.on_call(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def associate_user_info(req: https_fn.CallableRequest) -> dict[str, object]:
    """
    Associates custom information with the authenticated user in Firestore using the User model.
    Accepts:
      - github_access_token (optional)
      - gemini_api_key (optional)
      - last_assigned_issue_update_time (optional)
      - monitored_repos (optional)
      - custom_data / associated_data (optional)
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to associate information.",
        )

    uid = req.auth.uid
    token = req.auth.token
    provider_info = extract_provider_info(token)

    # Payload provided by caller
    payload: dict[str, object] = req.data if isinstance(req.data, dict) else {}

    # Extract user-specific fields
    raw_token = payload.get("github_access_token")
    github_access_token = str(raw_token) if raw_token is not None else None
    raw_key = payload.get("gemini_api_key")
    gemini_api_key = str(raw_key) if raw_key is not None else None
    raw_time = payload.get("last_assigned_issue_update_time")
    last_assigned_issue_update_time = str(raw_time) if raw_time is not None else None
    raw_repos = payload.get("monitored_repos")
    monitored_repos = [str(x) for x in raw_repos] if isinstance(raw_repos, list) else None
    raw_custom = payload.get("custom_data") or payload.get("associated_data")
    custom_data = raw_custom if isinstance(raw_custom, dict) else {}

    # Document reference in Firestore
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if doc_snap.exists:
        raw_user_dict = doc_snap.to_dict() or {}
        user = User.model_validate({**raw_user_dict, "uid": uid})

        # Update fields if provided
        if github_access_token is not None:
            user.github_access_token = github_access_token
        if gemini_api_key is not None:
            user.gemini_api_key = gemini_api_key
        if last_assigned_issue_update_time is not None:
            user.last_assigned_issue_update_time = last_assigned_issue_update_time
        if monitored_repos is not None:
            user.monitored_repos = monitored_repos
        if custom_data:
            user.custom_data.update(custom_data)

        # Ensure authentication and provider fields stay synced
        raw_email = token.get("email")
        if raw_email is not None:
            user.email = str(raw_email)
        raw_ver = token.get("email_verified")
        if raw_ver is not None:
            user.email_verified = bool(raw_ver)
        raw_name = token.get("name")
        if raw_name is not None:
            user.display_name = str(raw_name)
        raw_pic = token.get("picture")
        if raw_pic is not None:
            user.photo_url = str(raw_pic)

        raw_prov = provider_info.get("primary_provider")
        if raw_prov is not None:
            user.primary_provider = str(raw_prov)
        raw_gid = provider_info.get("google_id")
        if raw_gid is not None:
            user.google_id = str(raw_gid)
        raw_ghid = provider_info.get("github_id")
        if raw_ghid is not None:
            user.github_id = str(raw_ghid)
        raw_lp = provider_info.get("linked_providers")
        if isinstance(raw_lp, list):
            user.linked_providers = [str(x) for x in raw_lp]

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

    user_ref.set({**user.model_dump(), "updated_at": SERVER_TIMESTAMP}, merge=True)
    logger.info(f"User {action} in Firestore for UID {uid} (provider: {provider_info.get('primary_provider_name')})")

    return {
        "status": "success",
        "action": action,
        "uid": uid,
        "provider": provider_info.get("primary_provider_name"),
        "user": user.model_dump(mode="json"),
    }


@https_fn.on_call(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def get_user_info(req: https_fn.CallableRequest) -> dict[str, object]:
    """
    Retrieves the authenticated user's User document from Firestore.
    """
    if not req.auth or not req.auth.uid:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to retrieve information.",
        )

    uid = str(req.auth.uid)
    token = req.auth.token
    provider_info = extract_provider_info(token)

    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if not doc_snap.exists:
        user = User.from_auth_token(
            token_dict=token,
            provider_info=provider_info,
        )
        user_ref.set({**user.model_dump(), "updated_at": SERVER_TIMESTAMP})
    else:
        raw_user_dict = doc_snap.to_dict() or {}
        user = User.model_validate({**raw_user_dict, "uid": uid})

    return {
        "status": "success",
        "user": user.model_dump(mode="json"),
        "auth_provider": provider_info,
    }


@https_fn.on_call(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def sync_auth_profile(req: https_fn.CallableRequest) -> dict[str, object]:
    """
    Synchronizes Firebase Auth profile data into Firestore.
    """
    if not req.auth or not req.auth.uid:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED, message="User must be authenticated to sync profile."
        )

    uid = str(req.auth.uid)
    full_auth_record = fetch_full_user_auth_record(uid)
    provider_info = extract_provider_info(req.auth.token)

    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if doc_snap.exists:
        raw_user_dict = doc_snap.to_dict() or {}
        user = User.model_validate({**raw_user_dict, "uid": uid})
    else:
        user = User(uid=uid)

    user.email = str(full_auth_record["email"]) if full_auth_record.get("email") else None
    user.email_verified = bool(full_auth_record.get("email_verified", False))
    user.display_name = str(full_auth_record["display_name"]) if full_auth_record.get("display_name") else None
    user.photo_url = str(full_auth_record["photo_url"]) if full_auth_record.get("photo_url") else None
    user.primary_provider = str(provider_info["primary_provider"]) if provider_info.get("primary_provider") else None
    user.google_id = str(provider_info["google_id"]) if provider_info.get("google_id") else None
    user.github_id = str(provider_info["github_id"]) if provider_info.get("github_id") else None
    raw_linked = provider_info.get("linked_providers")
    user.linked_providers = [str(x) for x in raw_linked] if isinstance(raw_linked, list) else []

    user_ref.set({**user.model_dump(), "updated_at": SERVER_TIMESTAMP}, merge=True)
    logger.info(f"Synchronized User auth profile for UID {uid}")

    return {
        "status": "success",
        "message": "Auth profile synced successfully.",
        "user": user.model_dump(mode="json"),
        "auth_record": full_auth_record,
    }


@https_fn.on_call(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def delete_user_info(req: https_fn.CallableRequest) -> dict[str, object]:
    """
    Deletes the user's User document from Firestore.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to delete associated data.",
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


@https_fn.on_call(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def sync_github_issues(req: https_fn.CallableRequest) -> dict[str, object]:
    """
    Kicks off asynchronous GitHub issue synchronization in the background.
    Dispatches initial pagination tasks for assigned, mentioned, created, and monitored repo issues.
    """
    if not req.auth:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="User must be authenticated to sync GitHub issues.",
        )

    uid = req.auth.uid
    user_ref = db.collection("users").document(uid)
    doc_snap = user_ref.get()

    if not doc_snap.exists:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.NOT_FOUND,
            message="User document not found in Firestore. Please configure your GitHub access token first.",
        )

    raw_user_dict = doc_snap.to_dict() or {}
    user = User.model_validate({**raw_user_dict, "uid": uid})

    if not user.github_access_token:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.FAILED_PRECONDITION,
            message="User does not have a github_access_token configured.",
        )

    payload: dict[str, object] = req.data if isinstance(req.data, dict) else {}
    raw_state = payload.get("state")
    state = str(raw_state) if raw_state is not None else "open"

    try:
        result = start_user_github_sync(user=user, db=db, state=state)
        return result
    except Exception as e:
        logger.error(f"Error starting GitHub sync for UID {uid}: {e}")
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.INTERNAL, message=f"Failed to start GitHub sync: {e!s}"
        ) from e


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
    raw_url = data.get("url")
    url = str(raw_url) if raw_url is not None else None
    raw_params = data.get("params")
    params: dict[str, object] | None = raw_params if isinstance(raw_params, dict) else None
    raw_reason = data.get("reason", "assigned")
    reason = str(raw_reason)
    raw_owner = data.get("owner_fallback")
    owner_fallback = str(raw_owner) if raw_owner is not None else None
    raw_repo = data.get("repo_fallback")
    repo_fallback = str(raw_repo) if raw_repo is not None else None

    if not uid or not url:
        raise tasks_fn.HttpsError(
            code=tasks_fn.FunctionsErrorCode.INVALID_ARGUMENT, message="Missing 'uid' or 'url' in task data."
        )

    from github_sync import execute_issue_page_sync

    execute_issue_page_sync(
        uid=uid,
        url=url,
        params=params if params else None,
        reason=reason,
        owner_fallback=owner_fallback,
        repo_fallback=repo_fallback,
        db=db,
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

    logger.info(f"Executing asynchronous ranking for task {task_id} (UID {uid}) in Task Queue worker.")
    update_task_priority(uid=uid, task_id=task_id, db=db)


# ============================================================================
# Task & Priority Ranking Callable Functions
# ============================================================================


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
        logger.error(f"Error forcing task rerank for UID {uid}: {e}")
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
# HTTP REST API Function
# ============================================================================


@https_fn.on_request(cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "delete", "options"]))
def user_api(req: https_fn.Request) -> Response:
    """
    RESTful endpoint:
    - GET /: Returns User data.
    - POST /: Associates/updates User information.
    - DELETE /: Deletes User document.
    Requires header: 'Authorization: Bearer <ID_TOKEN>'.
    """
    if req.method == "OPTIONS":
        return Response("", status=204)

    auth_header = req.headers.get("Authorization")
    try:
        decoded_token = verify_bearer_token(auth_header)
    except https_fn.HttpsError as e:
        return Response(json.dumps({"error": e.message}), status=401, headers={"Content-Type": "application/json"})

    uid = str(decoded_token.get("uid") or "")
    provider_info = extract_provider_info(decoded_token)
    user_ref = db.collection("users").document(uid)

    if req.method == "GET":
        doc_snap = user_ref.get()
        if doc_snap.exists:
            raw_user_dict = doc_snap.to_dict() or {}
            user = User.model_validate({**raw_user_dict, "uid": uid})
        else:
            user = User.from_auth_token(decoded_token, provider_info)

        return Response(
            json.dumps(
                {
                    "status": "success",
                    "uid": uid,
                    "provider": provider_info.get("primary_provider_name"),
                    "user": user.model_dump(mode="json"),
                },
                default=str,
            ),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    elif req.method == "POST":
        try:
            body: dict[str, object] = req.get_json(silent=True) or {}
        except Exception:
            body = {}

        doc_snap = user_ref.get()
        if doc_snap.exists:
            raw_user_dict = doc_snap.to_dict() or {}
            user = User.model_validate({**raw_user_dict, "uid": uid})
        else:
            user = User.from_auth_token(decoded_token, provider_info)

        if "github_access_token" in body:
            user.github_access_token = (
                str(body["github_access_token"]) if body["github_access_token"] is not None else None
            )
        if "last_assigned_issue_update_time" in body:
            user.last_assigned_issue_update_time = (
                str(body["last_assigned_issue_update_time"])
                if body["last_assigned_issue_update_time"] is not None
                else None
            )
        if "monitored_repos" in body:
            raw_mr = body.get("monitored_repos")
            if isinstance(raw_mr, list):
                user.monitored_repos = [str(x) for x in raw_mr]
        if "custom_data" in body:
            raw_cd = body.get("custom_data")
            if isinstance(raw_cd, dict):
                user.custom_data.update(raw_cd)
        elif "associated_data" in body:
            raw_ad = body.get("associated_data")
            if isinstance(raw_ad, dict):
                user.custom_data.update(raw_ad)

        user_ref.set({**user.model_dump(), "updated_at": SERVER_TIMESTAMP}, merge=True)
        return Response(
            json.dumps(
                {
                    "status": "success",
                    "message": "User data updated successfully.",
                    "user": user.model_dump(mode="json"),
                }
            ),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    elif req.method == "DELETE":
        user_ref.delete()
        return Response(
            json.dumps({"status": "success", "message": f"User document for UID {uid} deleted."}),
            status=200,
            headers={"Content-Type": "application/json"},
        )

    return Response(
        json.dumps({"error": f"Method {req.method} not allowed."}),
        status=405,
        headers={"Content-Type": "application/json"},
    )


# ============================================================================
# Scheduled Functions: Closed Issues Sync & Task Cleanup (Option A)
# ============================================================================


@scheduler_fn.on_schedule(schedule="every 5 minutes", retry_count=1)
def scheduled_sync_closed_issues(event: scheduler_fn.ScheduledEvent) -> None:
    """
    Scheduled function that runs every 5 minutes to detect closed GitHub issues
    and remove them from users' task lists in Firestore.
    """
    logger.info("Starting scheduled 5-minute sync for closed GitHub issues.")
    sync_all_users_closed_issues(db=db)


# ============================================================================
# Cloud Firestore Triggers: Auto-Sync on User Settings Creation or Change
# ============================================================================


@firestore_fn.on_document_written(document="users/{uid}")
def on_user_settings_changed(
    event: firestore_fn.Event[firestore_fn.Change[firestore_fn.DocumentSnapshot | None]],
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
    if not after_snap.exists:
        logger.info("User document does not exist after write; skipping.")
        return

    after_data = after_snap.to_dict() or {}
    before_data = {}
    is_new = (before_snap is None) or not before_snap.exists
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
        if uid:
            logger.info(
                f"User settings created/changed for UID {uid} "
                f"(is_new={is_new}, token_changed={token_changed}, repos_changed={repos_changed}). "
                f"Triggering background GitHub sync."
            )
            user = User.model_validate({**after_data, "uid": str(uid)})
            start_user_github_sync(user=user, db=db, state="open")


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
    logger.info(f"[TRIGGER:on_task_written] Fired for users/{uid}/tasks/{task_id}")

    if event.data is None or event.data.after is None:
        logger.info(
            f"[TRIGGER:on_task_written] Task document was deleted for users/{uid}/tasks/{task_id}; skipping rerank trigger."
        )
        return

    after_snap = event.data.after
    before_snap = event.data.before

    # If document does not exist after write, do nothing
    if not after_snap.exists:
        logger.info(
            f"[TRIGGER:on_task_written] Task document does not exist after write for users/{uid}/tasks/{task_id}; skipping."
        )
        return

    after_data = after_snap.to_dict() or {}
    after_needs_update = after_data.get("priority_needs_updated", False)
    after_priority = after_data.get("priority", 0.0)

    is_new = (before_snap is None) or not before_snap.exists
    before_data = {}
    if not is_new and before_snap is not None:
        before_data = before_snap.to_dict() or {}
    before_needs_update = before_data.get("priority_needs_updated", False)
    before_priority = before_data.get("priority", 0.0)

    logger.info(
        f"[TRIGGER:on_task_written] users/{uid}/tasks/{task_id}: is_new={is_new}, "
        f"before(priority={before_priority}, needs_update={before_needs_update}), "
        f"after(priority={after_priority}, needs_update={after_needs_update})"
    )

    # Only trigger reranking if priority_needs_updated is True (prevents loop when ranker sets it to False)
    if not after_needs_update:
        logger.info(
            f"[TRIGGER:on_task_written] after_needs_update is False for users/{uid}/tasks/{task_id}; skipping rerank."
        )
        return

    if uid and task_id:
        logger.info(f"[TRIGGER:on_task_written] Enqueuing ranking for UID {uid}, task {task_id}.")
        enqueue_task_ranking(uid=str(uid), task_id=str(task_id), function_name="rank_user_tasks", db=db)
