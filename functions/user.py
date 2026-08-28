from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class User(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # User-specific application data
    github_access_token: str | None = None
    github_username: str | None = None
    gemini_api_key: str | None = None
    last_assigned_sync: datetime | None = None
    last_mentioned_sync: datetime | None = None
    last_created_sync: datetime | None = None
    monitored_repos: dict[str, datetime | None] = Field(default_factory=dict)

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

    # Timestamps
    created_at: datetime | None = None
    updated_at: datetime | None = None
