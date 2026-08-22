"""
Authentication utility helpers for Firebase Python backend.
Supports identifying and verifying Google and GitHub OAuth providers.
"""

from typing import Any, Dict, List, Optional
import logging
from firebase_admin import auth
from firebase_functions import https_fn

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {
    "google.com": "Google",
    "github.com": "GitHub",
}


def extract_provider_info(token_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts provider-specific information from a decoded Firebase ID token.
    """
    firebase_meta = token_dict.get("firebase", {})
    sign_in_provider = firebase_meta.get("sign_in_provider", "unknown")
    identities = firebase_meta.get("identities", {})

    linked_providers: List[str] = list(identities.keys())
    
    # Check if primary or linked providers include Google or GitHub
    supported_linked = [
        {"id": pid, "name": SUPPORTED_PROVIDERS.get(pid, pid)}
        for pid in linked_providers
        if pid in SUPPORTED_PROVIDERS
    ]

    return {
        "primary_provider": sign_in_provider,
        "primary_provider_name": SUPPORTED_PROVIDERS.get(sign_in_provider, sign_in_provider),
        "is_google": "google.com" in linked_providers or sign_in_provider == "google.com",
        "is_github": "github.com" in linked_providers or sign_in_provider == "github.com",
        "linked_providers": linked_providers,
        "supported_linked": supported_linked,
        "google_id": (identities.get("google.com") or [None])[0],
        "github_id": (identities.get("github.com") or [None])[0],
    }


def fetch_full_user_auth_record(uid: str) -> Dict[str, Any]:
    """
    Fetches the complete user record from Firebase Auth via Admin SDK,
    including providerData for Google and GitHub accounts.
    """
    try:
        user_record = auth.get_user(uid)
        providers = []
        for p in user_record.provider_data:
            provider_info = {
                "provider_id": p.provider_id,
                "provider_name": SUPPORTED_PROVIDERS.get(p.provider_id, p.provider_id),
                "uid": p.uid,
                "display_name": p.display_name,
                "email": p.email,
                "photo_url": p.photo_url,
            }
            providers.append(provider_info)

        return {
            "uid": user_record.uid,
            "email": user_record.email,
            "email_verified": user_record.email_verified,
            "display_name": user_record.display_name,
            "photo_url": user_record.photo_url,
            "disabled": user_record.disabled,
            "providers": providers,
            "custom_claims": user_record.custom_claims or {},
            "creation_timestamp": user_record.user_metadata.creation_timestamp,
            "last_sign_in_timestamp": user_record.user_metadata.last_sign_in_timestamp,
        }
    except Exception as e:
        logger.error(f"Error fetching auth user record for UID {uid}: {e}")
        return {"uid": uid, "error": str(e)}


def verify_bearer_token(auth_header: Optional[str]) -> Dict[str, Any]:
    """
    Validates standard Authorization: Bearer <ID_TOKEN> headers for HTTP endpoints.
    Raises HttpsError if invalid or missing.
    """
    if not auth_header:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="Missing Authorization header."
        )

    parts = auth_header.strip().split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="Invalid Authorization header format. Expected 'Bearer <token>'."
        )

    token = parts[1]
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token
    except auth.InvalidIdTokenError:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="Invalid Firebase ID token."
        )
    except auth.ExpiredIdTokenError:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message="Firebase ID token has expired."
        )
    except Exception as e:
        raise https_fn.HttpsError(
            code=https_fn.FunctionsErrorCode.UNAUTHENTICATED,
            message=f"Authentication failed: {str(e)}"
        )
