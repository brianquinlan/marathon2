from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class User(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    uid: str | None = None
    github_access_token: str | None = None
    github_username: str | None = None
    gemini_api_key: str | None = None
    last_assigned_sync: datetime | None = None
    last_mentioned_sync: datetime | None = None
    last_created_sync: datetime | None = None
    monitored_repos: dict[str, datetime | None] = Field(default_factory=dict)
