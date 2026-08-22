from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from google.cloud import firestore


@dataclass
class User:
    # User-specific application data
    github_access_token: Optional[str] = None
    last_assigned_issue_update_time: Optional[str] = None
    monitored_repos: List[str] = field(default_factory=list)

    # Firebase Authentication & Profile fields
    uid: Optional[str] = None
    email: Optional[str] = None
    email_verified: bool = False
    display_name: Optional[str] = None
    photo_url: Optional[str] = None
    primary_provider: Optional[str] = None
    google_id: Optional[str] = None
    github_id: Optional[str] = None
    linked_providers: List[str] = field(default_factory=list)

    # Additional custom associated metadata
    custom_data: Dict[str, Any] = field(default_factory=dict)

    # Timestamps
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None

    def to_dict(self, for_firestore: bool = True) -> Dict[str, Any]:
        """
        Converts the User dataclass to a dictionary for Firestore storage or JSON serialization.
        """
        data: Dict[str, Any] = {
            "uid": self.uid,
            "email": self.email,
            "email_verified": self.email_verified,
            "display_name": self.display_name,
            "photo_url": self.photo_url,
            "primary_provider": self.primary_provider,
            "google_id": self.google_id,
            "github_id": self.github_id,
            "linked_providers": self.linked_providers,
            "github_access_token": self.github_access_token,
            "last_assigned_issue_update_time": self.last_assigned_issue_update_time,
            "monitored_repos": self.monitored_repos,
            "custom_data": self.custom_data,
        }

        if for_firestore:
            data["updated_at"] = firestore.SERVER_TIMESTAMP
            if self.created_at is None:
                data["created_at"] = firestore.SERVER_TIMESTAMP
            else:
                data["created_at"] = self.created_at
        else:
            # Handle ISO string conversions for serialized output
            for ts_field in ["created_at", "updated_at"]:
                val = getattr(self, ts_field, None)
                if val is not None and hasattr(val, "isoformat"):
                    data[ts_field] = val.isoformat()
                else:
                    data[ts_field] = val

        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any], uid: Optional[str] = None) -> "User":
        """
        Instantiates a User dataclass from a Firestore document dictionary.
        """
        return cls(
            uid=data.get("uid") or uid,
            email=data.get("email"),
            email_verified=data.get("email_verified", False),
            display_name=data.get("display_name"),
            photo_url=data.get("photo_url"),
            primary_provider=data.get("primary_provider"),
            google_id=data.get("google_id"),
            github_id=data.get("github_id"),
            linked_providers=data.get("linked_providers") or [],
            github_access_token=data.get("github_access_token"),
            last_assigned_issue_update_time=data.get("last_assigned_issue_update_time"),
            monitored_repos=data.get("monitored_repos") or [],
            custom_data=data.get("custom_data") or {},
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

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