"""Application persistence."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from database.models import Application
from schemas.application import ApplicationSchema

logger = logging.getLogger(__name__)


class ApplicationRepository:
    """Transaction boundaries belong to the caller, not here."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_guid(self, content_guid: str) -> Optional[Application]:
        stmt = select(Application).where(Application.content_guid == content_guid)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_all(self, *, limit: Optional[int] = None) -> List[Application]:
        stmt = select(Application).order_by(Application.app_name.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    def count(self) -> int:
        return int(self._session.execute(select(func.count(Application.id))).scalar_one())

    def distinct_owner_count(self) -> int:
        stmt = select(func.count(func.distinct(Application.owner))).where(
            Application.owner.is_not(None)
        )
        return int(self._session.execute(stmt).scalar_one())

    def scan_time_range(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        row = self._session.execute(
            select(
                func.min(Application.last_inventory_scan),
                func.max(Application.last_inventory_scan),
            )
        ).one()
        return row[0], row[1]

    def upsert_many(
        self, applications: Sequence[ApplicationSchema], scanned_at: datetime
    ) -> int:
        if not applications:
            return 0

        # ON CONFLICT can't resolve two conflicting rows in the same INSERT,
        # so collapse duplicates first.
        deduped: Dict[str, Dict[str, Any]] = {}
        for app in applications:
            row = self._to_row(app, scanned_at)
            deduped[row["content_guid"]] = row

        stmt = pg_insert(Application).values(list(deduped.values()))
        stmt = stmt.on_conflict_do_update(
            index_elements=[Application.content_guid],
            set_={
                "app_name": stmt.excluded.app_name,
                "owner": stmt.excluded.owner,
                "owner_email": stmt.excluded.owner_email,
                "content_url": stmt.excluded.content_url,
                "bundle_id": stmt.excluded.bundle_id,
                "created_at": stmt.excluded.created_at,
                "updated_at": stmt.excluded.updated_at,
                "last_inventory_scan": stmt.excluded.last_inventory_scan,
            },
        )
        self._session.execute(stmt)
        return len(deduped)

    def upsert(self, application: ApplicationSchema, scanned_at: datetime) -> int:
        return self.upsert_many([application], scanned_at)

    def delete_missing(self, live_guids: Sequence[str]) -> int:
        """Only safe after a complete scan; a partial listing would delete live content."""
        if not live_guids:
            return 0
        stale = list(
            self._session.execute(
                select(Application).where(Application.content_guid.notin_(live_guids))
            )
            .scalars()
            .all()
        )
        for record in stale:
            logger.info(
                "Removing application no longer on Connect",
                extra={"content_guid": record.content_guid, "app_name": record.app_name},
            )
            self._session.delete(record)
        self._session.flush()
        return len(stale)

    @staticmethod
    def _to_row(app: ApplicationSchema, scanned_at: datetime) -> Dict[str, Any]:
        return {
            "content_guid": app.content_guid,
            "app_name": app.app_name,
            "owner": app.resolved_owner(),
            "owner_email": app.owner_email,
            "content_url": app.content_url,
            "bundle_id": app.bundle_id,
            "created_at": app.created_at,
            "updated_at": app.updated_at,
            "last_inventory_scan": scanned_at,
        }
