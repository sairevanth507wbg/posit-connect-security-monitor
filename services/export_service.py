"""CSV export of the inventory, for handoff to the OIS scanning tool."""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from sqlalchemy import select

from database.connection import Database
from database.models import Application, Package

logger = logging.getLogger(__name__)

# One row per (application, package). Flat rather than two files so the scanner
# can attribute every finding to an owner without a join on their side.
INVENTORY_COLUMNS: Tuple[str, ...] = (
    "application_name",
    "content_guid",
    "owner",
    "owner_email",
    "content_url",
    "bundle_id",
    "app_created_at",
    "app_last_deployed",
    "package_name",
    "package_version",
    "package_type",
    "inventory_scanned_at",
)

APPLICATION_COLUMNS: Tuple[str, ...] = (
    "application_name",
    "content_guid",
    "owner",
    "owner_email",
    "content_url",
    "bundle_id",
    "app_created_at",
    "app_last_deployed",
    "package_count",
    "inventory_scanned_at",
)


def _iso(value: Optional[datetime]) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


class ExportService:
    """Reads the stored inventory and renders it as CSV."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def inventory_rows(self) -> Iterator[List[str]]:
        """Yield one row per package per application, ordered for readability."""
        stmt = (
            select(Application, Package)
            .join(Package, Package.content_guid == Application.content_guid)
            .order_by(
                Application.app_name.asc(),
                Package.package_type.asc(),
                Package.package_name.asc(),
            )
        )
        with self._database.session() as session:
            for app, package in session.execute(stmt).all():
                yield [
                    app.app_name,
                    app.content_guid,
                    app.owner or "",
                    app.owner_email or "",
                    app.content_url or "",
                    app.bundle_id or "",
                    _iso(app.created_at),
                    _iso(app.updated_at),
                    package.package_name,
                    package.package_version,
                    package.package_type,
                    _iso(package.scanned_at),
                ]

    def application_rows(self) -> Iterator[List[str]]:
        """Yield one row per application, with a package count."""
        stmt = select(Application).order_by(Application.app_name.asc())
        with self._database.session() as session:
            for app in session.execute(stmt).scalars().all():
                yield [
                    app.app_name,
                    app.content_guid,
                    app.owner or "",
                    app.owner_email or "",
                    app.content_url or "",
                    app.bundle_id or "",
                    _iso(app.created_at),
                    _iso(app.updated_at),
                    str(len(app.packages)),
                    _iso(app.last_inventory_scan),
                ]

    def to_string(self, *, applications_only: bool = False) -> str:
        """Render the CSV into memory. Suitable for a few hundred thousand rows."""
        buffer = io.StringIO()
        # QUOTE_MINIMAL with \r\n keeps Excel and pandas both happy.
        writer = csv.writer(buffer, lineterminator="\r\n")
        if applications_only:
            writer.writerow(APPLICATION_COLUMNS)
            rows = self.application_rows()
        else:
            writer.writerow(INVENTORY_COLUMNS)
            rows = self.inventory_rows()

        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1

        logger.info("Rendered inventory CSV", extra={"rows": count})
        return buffer.getvalue()

    def to_file(self, path: Path, *, applications_only: bool = False) -> int:
        """Write the CSV to disk. Returns the number of data rows written."""
        path = Path(path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        columns = APPLICATION_COLUMNS if applications_only else INVENTORY_COLUMNS
        rows = self.application_rows() if applications_only else self.inventory_rows()

        count = 0
        # newline="" is required by the csv module; utf-8-sig so Excel on
        # Windows opens non-ASCII names correctly instead of mojibake.
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle, lineterminator="\r\n")
            writer.writerow(columns)
            for row in rows:
                writer.writerow(row)
                count += 1

        logger.info("Wrote inventory CSV", extra={"path": str(path), "rows": count})
        return count

    @staticmethod
    def suggested_filename(prefix: str = "connect-inventory") -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return prefix + "-" + stamp + ".csv"
