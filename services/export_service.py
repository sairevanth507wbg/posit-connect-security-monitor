"""CSV and zipped-CSV export of the inventory, for handoff to the OIS scanning tool."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import selectinload

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


# Wiz parses these purl ecosystems; anything else is inventoried without a
# purl, which lists it in the SBOM but leaves it unmatched by the scanner.
PURL_ECOSYSTEMS: Dict[str, str] = {
    "Python": "pypi",
    "R": "cran",
}

CYCLONEDX_SPEC_VERSION = "1.6"
TOOL_NAME = "posit-connect-security-monitor"


def _purl(name: str, version: str, package_type: str) -> Optional[str]:
    """Build a package URL, or None for an ecosystem no scanner can resolve."""
    ecosystem = PURL_ECOSYSTEMS.get(package_type)
    if not ecosystem or not name:
        return None

    # The purl spec lowercases PyPI names and maps underscore to hyphen.
    # CRAN names are case-sensitive and must be left alone.
    if ecosystem == "pypi":
        name = name.lower().replace("_", "-")

    purl = "pkg:" + ecosystem + "/" + quote(name, safe="")
    if version:
        purl += "@" + quote(version, safe="")
    return purl


def _slug(value: str, fallback: str) -> str:
    """Filename-safe form of an application name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-.")
    return (cleaned or fallback or "app")[:80]


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

    # ---- CycloneDX SBOM -------------------------------------------------

    def _cyclonedx_document(self, app: Any, packages: Sequence[Any]) -> Dict[str, Any]:
        """Build one CycloneDX 1.6 document for a single application."""
        components: List[Dict[str, Any]] = []
        seen = set()
        for package in packages:
            purl = _purl(
                package.package_name, package.package_version, package.package_type
            )
            ref = purl or (
                package.package_name
                + "@"
                + (package.package_version or "unknown")
                + "?type="
                + package.package_type
            )
            if ref in seen:
                continue
            seen.add(ref)

            component: Dict[str, Any] = {
                "type": "library",
                "bom-ref": ref,
                "name": package.package_name,
                "version": package.package_version or "unknown",
            }
            if purl:
                component["purl"] = purl
            else:
                # Quarto and Unknown have no purl ecosystem. Listing them keeps
                # the SBOM a complete inventory even though Wiz cannot match
                # them against advisories.
                component["properties"] = [
                    {"name": "connect:package_type", "value": package.package_type}
                ]
            components.append(component)

        # content_guid is the join key: findings come back per SBOM, and this
        # is what maps them to the application and therefore to its owner.
        # Owner email is deliberately omitted so no PII leaves the system.
        properties = [{"name": "connect:content_guid", "value": app.content_guid}]
        if app.owner:
            properties.append({"name": "connect:owner", "value": app.owner})
        if app.content_url:
            properties.append({"name": "connect:content_url", "value": app.content_url})

        return {
            "bomFormat": "CycloneDX",
            "specVersion": CYCLONEDX_SPEC_VERSION,
            "serialNumber": "urn:uuid:" + str(uuid.uuid4()),
            "version": 1,
            "metadata": {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tools": {"components": [{"type": "application", "name": TOOL_NAME}]},
                "component": {
                    "type": "application",
                    "bom-ref": app.content_guid,
                    "name": app.app_name,
                    "version": app.bundle_id or "unknown",
                    "properties": properties,
                },
            },
            "components": components,
        }

    def cyclonedx_documents(self) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Yield (filename, document) for every application, one SBOM each."""
        stmt = (
            select(Application)
            .options(selectinload(Application.packages))
            .order_by(Application.app_name.asc())
        )
        with self._database.session() as session:
            for app in session.execute(stmt).scalars().all():
                # Names collide across owners, so the guid prefix keeps the
                # filename unique per application as OIS asked.
                filename = (
                    _slug(app.app_name, app.content_guid)
                    + "-"
                    + app.content_guid[:8]
                    + "-cyclonedx.json"
                )
                yield filename, self._cyclonedx_document(app, app.packages)

    def to_sbom_zip_bytes(self) -> Tuple[bytes, int]:
        """Zip one CycloneDX document per application. Returns (bytes, count)."""
        buffer = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, document in self.cyclonedx_documents():
                archive.writestr(
                    filename, json.dumps(document, indent=2, ensure_ascii=False)
                )
                count += 1
        return buffer.getvalue(), count

    def to_sbom_zip(self, path: Path) -> int:
        """Write per-application SBOMs to a zip. Returns the document count."""
        path = Path(path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        data, count = self.to_sbom_zip_bytes()
        path.write_bytes(data)
        logger.info(
            "Wrote CycloneDX SBOM bundle",
            extra={"path": str(path), "documents": count, "bytes": len(data)},
        )
        return count

    @staticmethod
    def suggested_filename(
        prefix: str = "connect-inventory", extension: str = "csv"
    ) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return prefix + "-" + stamp + "." + extension.lstrip(".")

    @staticmethod
    def _zip_bytes(csv_text: str, arcname: str) -> bytes:
        """Wrap rendered CSV text in a single-entry deflated archive."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            # utf-8-sig to match to_file(), so the extracted CSV opens cleanly
            # in Excel rather than as mojibake.
            archive.writestr(arcname, csv_text.encode("utf-8-sig"))
        return buffer.getvalue()

    def to_zip_bytes(
        self,
        *,
        applications_only: bool = False,
        arcname: Optional[str] = None,
    ) -> bytes:
        """Render the CSV and return it zipped, in memory."""
        csv_text = self.to_string(applications_only=applications_only)
        return self._zip_bytes(csv_text, arcname or self.suggested_filename())

    def to_zip_file(
        self,
        path: Path,
        *,
        applications_only: bool = False,
        arcname: Optional[str] = None,
    ) -> int:
        """Write the CSV to disk inside a zip. Returns the number of data rows."""
        path = Path(path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        csv_text = self.to_string(applications_only=applications_only)
        data = self._zip_bytes(csv_text, arcname or self.suggested_filename())
        path.write_bytes(data)

        # to_string() terminates every row, header included, with CRLF.
        count = max(csv_text.count("\r\n") - 1, 0)
        logger.info(
            "Wrote inventory zip",
            extra={"path": str(path), "rows": count, "bytes": len(data)},
        )
        return count
