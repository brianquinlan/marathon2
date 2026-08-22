from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
from google.cloud import firestore


class User(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # User-specific application data
    github_access_token: Optional[str] = None
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
    custom_data: Dict[str, Any] = Field(default_factory=dict)

    # Timestamps
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None

    @classmethod
    def from_auth_token(
        cls,
        token_dict: Dict[str, Any],
        provider_info: Dict[str, Any],
        github_access_token: Optional[str] = None,
        last_assigned_issue_update_time: Optional[str] = None,
        monitored_repos: Optional[List[str]] = None,
        custom_data: Optional[Dict[str, Any]] = None,
    ) -> "User":
        """
        Creates a User dataclass instance populated with authentication claims from a decoded Firebase ID token.
        """
        return cls(
            uid=token_dict.get("uid"),
            email=token_dict.get("email"),
            email_verified=token_dict.get("email_verified", False),
            display_name=token_dict.get("name"),
            photo_url=token_dict.get("picture"),
            primary_provider=provider_info.get("primary_provider"),
            google_id=provider_info.get("google_id"),
            github_id=provider_info.get("github_id"),
            linked_providers=provider_info.get("linked_providers", []),
            github_access_token=github_access_token,
            last_assigned_issue_update_time=last_assigned_issue_update_time,
            monitored_repos=monitored_repos or [],
            custom_data=custom_data or {},
        )