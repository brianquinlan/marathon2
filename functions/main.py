"""
Firebase Functions (2nd Gen) Python Backend
Associates and manages user-specific information for users authenticated via
Firebase Authentication (Google and GitHub providers supported) using the User dataclass.
"""

from typing import Any, Dict, Optional
import datetime
import json
import logging

from firebase_functions import https_fn, options
import firebase_admin
from firebase_admin import credentials, firestore, auth

from auth_utils import extract_provider_info, fetch_full_user_auth_record, verify_bearer_token
from user import User

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Firebase Admin App if not already initialized
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()


# ============================================================================
# Callable Functions (for Web / iOS / Android / Flutter Firebase Client SDKs)
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
    If the document does not exist yet, it is automatically initialized from Firebase Auth.
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
        # Auto-initialize user model
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
    Synchronizes Firebase Auth profile data (including latest Google/GitHub provider metadata)
    into the User document in Firestore.
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
# HTTP REST API Function (for REST / cURL / External Clients)
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
