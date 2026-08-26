"""Application and user schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from schemas.package import PackageSchema


class UserSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    guid: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None

    def display_name(self) -> Optional[str]:
        full_name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return full_name or self.username or self.guid


class ApplicationSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    content_guid: str = Field(
        validation_alias=AliasChoices("guid", "content_guid"), min_length=1
    )
    app_name: str
    owner: Optional[str] = None
    content_url: Optional[str] = None
    bundle_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("bundle_id", "bundleId")
    )
    created_at: Optional[datetime] = Field(
        default=None, validation_alias=AliasChoices("created_time", "created_at")
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        validation_alias=AliasChoices("last_deployed_time", "updated_time", "updated_at"),
    )

    owner_guid: Optional[str] = None
    owner_username: Optional[str] = None
    owner_first_name: Optional[str] = None
    owner_last_name: Optional[str] = None
    owner_email: Optional[str] = None
    app_mode: Optional[str] = None

    packages: List[PackageSchema] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _derive_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)

        # Connect exposes 'name' as a URL slug and 'title' as the label.
        title = (payload.get("title") or "").strip()
        slug = (payload.get("name") or "").strip()
        guid = (payload.get("guid") or payload.get("content_guid") or "").strip()
        payload["app_name"] = title or slug or guid or "(untitled)"

        url = payload.get("content_url") or payload.get("dashboard_url") or payload.get("url")
        payload["content_url"] = url.strip() if isinstance(url, str) and url.strip() else None

        bundle = payload.get("bundle_id", payload.get("bundleId"))
        payload["bundle_id"] = str(bundle) if bundle not in (None, "") else None

        # ?include=owner returns a nested object on newer servers.
        embedded = payload.get("owner")
        if isinstance(embedded, dict):
            payload["owner_username"] = embedded.get("username")
            payload["owner_first_name"] = embedded.get("first_name")
            payload["owner_last_name"] = embedded.get("last_name")
            payload["owner_email"] = embedded.get("email")
            payload.setdefault("owner_guid", embedded.get("guid"))
            payload["owner"] = None

        return payload

    def resolved_owner(self) -> Optional[str]:
        if self.owner:
            return self.owner
        full_name = " ".join(
            p for p in (self.owner_first_name, self.owner_last_name) if p
        ).strip()
        return full_name or self.owner_username or self.owner_guid

    def fallback_content_url(self, server_url: str) -> Optional[str]:
        if self.content_url:
            return self.content_url
        if not self.content_guid:
            return None
        return server_url.rstrip("/") + "/connect/#/apps/" + self.content_guid
