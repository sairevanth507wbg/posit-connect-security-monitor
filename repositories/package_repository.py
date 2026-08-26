"""Package persistence."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from database.models import Package
from schemas.package import PackageSchema

logger = logging.getLogger(__name__)


class PackageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_content(self, content_guid: str) -> List[Package]:
        stmt = (
            select(Package)
            .where(Package.content_guid == content_guid)
            .order_by(Package.package_type.asc(), Package.package_name.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def count(self) -> int:
        return int(self._session.execute(select(func.count(Package.id))).scalar_one())

    def count_for_content(self, content_guid: str) -> int:
        stmt = select(func.count(Package.id)).where(Package.content_guid == content_guid)
        return int(self._session.execute(stmt).scalar_one())

    def distinct_package_versions(self) -> int:
        """Work-list size for the Phase 2 vulnerability lookups."""
        subquery = (
            select(Package.package_name, Package.package_version, Package.package_type)
            .distinct()
            .subquery()
        )
        return int(
            self._session.execute(select(func.count()).select_from(subquery)).scalar_one()
        )

    def package_type_breakdown(self) -> Dict[str, int]:
        stmt = select(Package.package_type, func.count(Package.id)).group_by(
            Package.package_type
        )
        return {row[0]: int(row[1]) for row in self._session.execute(stmt).all()}

    def sync_for_content(
        self,
        content_guid: str,
        packages: Sequence[PackageSchema],
        scanned_at: datetime,
    ) -> int:
        """Upsert what Connect reports, then drop whatever this scan didn't touch.

        scanned_at must be identical across the whole run - stale detection
        compares against it.
        """
        deduped = self._dedupe(packages)

        if deduped:
            rows: List[Dict[str, Any]] = [
                {
                    "content_guid": content_guid,
                    "package_name": package.package_name,
                    "package_version": package.package_version,
                    "package_type": package.package_type.value,
                    "scanned_at": scanned_at,
                }
                for package in deduped.values()
            ]
            stmt = pg_insert(Package).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_packages_identity",
                set_={"scanned_at": stmt.excluded.scanned_at},
            )
            self._session.execute(stmt)

        self._session.execute(
            delete(Package).where(
                Package.content_guid == content_guid,
                Package.scanned_at < scanned_at,
            )
        )
        return len(deduped)

    def delete_for_content(self, content_guid: str) -> int:
        result = self._session.execute(
            delete(Package).where(Package.content_guid == content_guid)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def _dedupe(
        packages: Sequence[PackageSchema],
    ) -> Dict[Tuple[str, str, str], PackageSchema]:
        """Connect can list the same package twice (direct and transitive).

        PostgreSQL rejects an INSERT holding two rows with the same conflict
        key, so they have to be collapsed before the statement is built.
        """
        deduped: Dict[Tuple[str, str, str], PackageSchema] = {}
        for package in packages:
            if not package.package_name:
                continue
            deduped.setdefault(package.identity, package)
        return deduped
