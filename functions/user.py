from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict, field_serializer


class User(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # User-specific application data
    github_access_token: Optional[str] = None
    github_username: Optional[str] = None
    gemini_api_key: Optional[str] = None
    last_assigned_issue_update_time: Optional[str] = None
    monitored_repos: List[str] = Field(default_factory=list)

    # Firebase Authentication & Profile fields
    uid: Optional[str] = None
    email: Optional[str] = None
    email_verified: bool = False
    display_name: Optional[str] = None
    photo_url: Optional[str] = None
    primary_provider: Optional[str] = None
    google_id: Optional[str] = None
    github_id: Optional[str] = None
    linked_providers: List[str] = Field(default_factory=list)

    # Additional custom associated metadata
    custom_data: Dict[str, object] = Field(default_factory=dict)

    # Timestamps (can be datetime, ISO string, or Firestore SERVER_TIMESTAMP Sentinel)
    created_at: Optional[datetime | str | object] = None
    updated_at: Optional[datetime | str | object] = None

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_timestamps(self, v: Optional[datetime | str | object]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)

    @classmethod
    def from_auth_token(
        cls,
        token_dict: Dict[str, object],
        provider_info: Dict[str, object],
        github_access_token: Optional[str] = None,
        last_assigned_issue_update_time: Optional[str] = None,
        monitored_repos: Optional[List[str]] = None,
        custom_data: Optional[Dict[str, object]] = None,
    ) -> "User":
        """
        Creates a User dataclass instance populated with authentication claims from a decoded Firebase ID token.
        """
        raw_linked = provider_info.get("linked_providers")
        linked_list: List[str] = [str(x) for x in raw_linked] if isinstance(raw_linked, list) else []

        raw_verified = token_dict.get("email_verified")
        email_verified = bool(raw_verified) if raw_verified is not None else False

        raw_uid = token_dict.get("uid")
        raw_email = token_dict.get("email")
        raw_name = token_dict.get("name")
        raw_pic = token_dict.get("picture")
        raw_provider = provider_info.get("primary_provider")
        raw_google_id = provider_info.get("google_id")
        raw_github_id = provider_info.get("github_id")

        return cls(
            uid=str(raw_uid) if raw_uid is not None else None,
            email=str(raw_email) if raw_email is not None else None,
            email_verified=email_verified,
            display_name=str(raw_name) if raw_name is not None else None,
            photo_url=str(raw_pic) if raw_pic is not None else None,
            primary_provider=str(raw_provider) if raw_provider is not None else None,
            google_id=str(raw_google_id) if raw_google_id is not None else None,
            github_id=str(raw_github_id) if raw_github_id is not None else None,
            linked_providers=linked_list,
            github_access_token=github_access_token,
            last_assigned_issue_update_time=last_assigned_issue_update_time,
            monitored_repos=monitored_repos or [],
            custom_data=custom_data or {},
        )