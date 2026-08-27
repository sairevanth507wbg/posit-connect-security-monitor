"""ORM models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        Index("ix_applications_owner", "owner"),
        Index("ix_applications_owner_email", "owner_email"),
        Index("ix_applications_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_guid: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    app_name: Mapped[str] = mapped_column(String(512), nullable=False)
    owner: Mapped[Optional[str]] = mapped_column(String(256))
    owner_email: Mapped[Optional[str]] = mapped_column(String(320))
    content_url: Mapped[Optional[str]] = mapped_column(String(1024))
    bundle_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_inventory_scan: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    packages: Mapped[List["Package"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return "<Application " + self.content_guid + " " + self.app_name + ">"


class Finding(Base):
    """One vulnerability, in one package, in one application."""

    __tablename__ = "findings"
    __table_args__ = (
        # Wiz issues no stable finding id across scans, so this tuple is the
        # identity. It lets a re-scan update a finding instead of duplicating it.
        UniqueConstraint(
            "content_guid",
            "package_name",
            "package_version",
            "vulnerability_id",
            name="uq_findings_identity",
        ),
        Index("ix_findings_severity", "severity"),
        Index("ix_findings_vulnerability", "vulnerability_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_guid: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("applications.content_guid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    package_name: Mapped[str] = mapped_column(String(256), nullable=False)
    package_version: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    package_type: Mapped[Optional[str]] = mapped_column(String(32))

    vulnerability_id: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[Optional[str]] = mapped_column(String(16))
    fixed_version: Mapped[Optional[str]] = mapped_column(String(128))
    summary: Mapped[Optional[str]] = mapped_column(String(1024))
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="wiz")

    # first_seen never moves; last_seen advances on every scan that still
    # reports it, which is how a fixed finding becomes visible as stale.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return "<Finding " + self.vulnerability_id + " " + self.package_name + ">"


class Notification(Base):
    """A record that one owner was told about one finding.

    Written before the send, so a crash mid-send risks a missed alert rather
    than mailing the same person twice.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "owner_email",
            "content_guid",
            "package_name",
            "package_version",
            "vulnerability_id",
            name="uq_notifications_identity",
        ),
        Index("ix_notifications_sent_at", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    content_guid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    package_name: Mapped[str] = mapped_column(String(256), nullable=False)
    package_version: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    vulnerability_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # "dry_run" rows record what would have been sent without claiming the
    # owner was told, so switching to live sending still notifies them.
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="sent")
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return "<Notification " + self.owner_email + " " + self.vulnerability_id + ">"


class Package(Base):
    __tablename__ = "packages"
    __table_args__ = (
        # Makes ON CONFLICT upserts possible and blocks duplicate rows.
        UniqueConstraint(
            "content_guid",
            "package_name",
            "package_version",
            "package_type",
            name="uq_packages_identity",
        ),
        Index("ix_packages_name_version", "package_name", "package_version"),
        Index("ix_packages_type", "package_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_guid: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("applications.content_guid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    package_name: Mapped[str] = mapped_column(String(256), nullable=False)
    package_version: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default=""
    )
    package_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    application: Mapped["Application"] = relationship(back_populates="packages")

    def __repr__(self) -> str:
        return "<Package " + self.package_name + " " + self.package_version + ">"
