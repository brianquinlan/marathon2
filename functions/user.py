from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class User(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # User-specific application data
    github_access_token: str | None = None
    github_username: str | None = None
    gemini_api_key: str | None = None
    last_assigned_issue_update_time: str | None = None
    monitored_repos: list[str] = Field(default_factory=list)

    # Firebase Authentication & Profile fields
    uid: str | None = None
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    photo_url: str | None = None
    primary_provider: str | None = None
    google_id: str | None = None
    github_id: str | None = None
    linked_providers: list[str] = Field(default_factory=list)

    # Additional custom associated metadata
    custom_data: dict[str, object] = Field(default_factory=dict)

    # Timestamps (can be datetime, ISO string, or Firestore SERVER_TIMESTAMP Sentinel)
    created_at: datetime | str | object | None = None
    updated_at: datetime | str | object | None = None

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_timestamps(self, v: datetime | str | object | None) -> str | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)

    @classmethod
    def from_auth_token(
        cls,
        token_dict: dict[str, object],
        provider_info: dict[str, object],
        github_access_token: str | None = None,
        last_assigned_issue_update_time: str | None = None,
        monitored_repos: list[str] | None = None,
        custom_data: dict[str, object] | None = None,
    ) -> "User":
        """
        Creates a User dataclass instance populated with authentication claims from a decoded Firebase ID token.
        """
        raw_linked = provider_info.get("linked_providers")
        linked_list: list[str] = [str(x) for x in raw_linked] if isinstance(raw_linked, list) else []

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
