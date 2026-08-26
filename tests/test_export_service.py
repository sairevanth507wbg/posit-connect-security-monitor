"""Tests for the CSV export handed to the OIS scanning team.

The export uses portable SELECT/JOIN only, so it runs against SQLite here.
The PostgreSQL-specific upserts live in the repositories and are covered
separately.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Application, Base, Package
from services.export_service import (
    APPLICATION_COLUMNS,
    INVENTORY_COLUMNS,
    ExportService,
)

SCANNED_AT = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class SqliteDatabase:
    """Minimal stand-in for Database, backed by in-memory SQLite."""

    def __init__(self) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self):
        from contextlib import contextmanager

        @contextmanager
        def _session():
            s = self._factory()
            try:
                yield s
                s.commit()
            finally:
                s.close()

        return _session()


@pytest.fixture
def database():
    db = SqliteDatabase()
    with db.session() as session:
        session.add(
            Application(
                content_guid="c-treasury",
                app_name="Treasury Dashboard",
                owner="Sai Revanth",
                owner_email="sai@worldbank.org",
                content_url="https://connect.example.org/content/c-treasury/",
                bundle_id="27776",
                created_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
                updated_at=datetime(2025, 6, 2, tzinfo=timezone.utc),
                last_inventory_scan=SCANNED_AT,
            )
        )
        session.add(
            Application(
                content_guid="c-static",
                app_name="Static Docs",
                owner="Jane Doe",
                owner_email=None,
                content_url=None,
                bundle_id=None,
                last_inventory_scan=SCANNED_AT,
            )
        )
        session.add_all(
            [
                Package(content_guid="c-treasury", package_name="pandas",
                        package_version="2.2.2", package_type="Python",
                        scanned_at=SCANNED_AT),
                Package(content_guid="c-treasury", package_name="numpy",
                        package_version="2.1.0", package_type="Python",
                        scanned_at=SCANNED_AT),
                Package(content_guid="c-treasury", package_name="dplyr",
                        package_version="1.1.4", package_type="R",
                        scanned_at=SCANNED_AT),
                # Unpinned version - must survive the round trip as empty.
                Package(content_guid="c-treasury", package_name="requests",
                        package_version="", package_type="Python",
                        scanned_at=SCANNED_AT),
            ]
        )
    return db


def parse(text: str):
    return list(csv.reader(io.StringIO(text)))


class TestInventoryExport:
    def test_header_matches_declared_columns(self, database):
        rows = parse(ExportService(database).to_string())
        assert tuple(rows[0]) == INVENTORY_COLUMNS

    def test_one_row_per_package(self, database):
        rows = parse(ExportService(database).to_string())
        assert len(rows) - 1 == 4  # header excluded

    def test_application_fields_repeat_on_every_package_row(self, database):
        rows = parse(ExportService(database).to_string())
        header, data = rows[0], rows[1:]
        name_i = header.index("application_name")
        email_i = header.index("owner_email")
        assert all(r[name_i] == "Treasury Dashboard" for r in data)
        assert all(r[email_i] == "sai@worldbank.org" for r in data)

    def test_owner_email_is_exported(self, database):
        """OIS needs this to route findings back to the owner."""
        text = ExportService(database).to_string()
        assert "sai@worldbank.org" in text

    def test_nulls_become_empty_strings_not_none(self, database):
        rows = parse(ExportService(database).to_string(applications_only=True))
        header, data = rows[0], rows[1:]
        static = next(r for r in data if r[header.index("application_name")] == "Static Docs")
        assert static[header.index("owner_email")] == ""
        assert static[header.index("content_url")] == ""
        assert "None" not in static

    def test_unpinned_version_stays_empty(self, database):
        rows = parse(ExportService(database).to_string())
        header, data = rows[0], rows[1:]
        req = next(r for r in data if r[header.index("package_name")] == "requests")
        assert req[header.index("package_version")] == ""

    def test_timestamps_are_iso8601(self, database):
        rows = parse(ExportService(database).to_string())
        header, data = rows[0], rows[1:]
        value = data[0][header.index("app_created_at")]
        assert value.startswith("2024-01-15T")
        datetime.fromisoformat(value)

    def test_sorted_by_application_then_type_then_name(self, database):
        rows = parse(ExportService(database).to_string())
        header, data = rows[0], rows[1:]
        names = [r[header.index("package_name")] for r in data]
        assert names == ["numpy", "pandas", "requests", "dplyr"]

    def test_crlf_line_endings_for_excel(self, database):
        assert "\r\n" in ExportService(database).to_string()


class TestApplicationExport:
    def test_one_row_per_application(self, database):
        rows = parse(ExportService(database).to_string(applications_only=True))
        assert tuple(rows[0]) == APPLICATION_COLUMNS
        assert len(rows) - 1 == 2

    def test_package_count_included(self, database):
        rows = parse(ExportService(database).to_string(applications_only=True))
        header, data = rows[0], rows[1:]
        counts = {r[header.index("application_name")]: r[header.index("package_count")]
                  for r in data}
        assert counts == {"Treasury Dashboard": "4", "Static Docs": "0"}


class TestFileOutput:
    def test_writes_file_with_bom_for_excel(self, database, tmp_path):
        path = tmp_path / "out" / "inventory.csv"
        count = ExportService(database).to_file(path)
        assert count == 4
        assert path.exists()
        # utf-8-sig BOM so Excel on Windows renders non-ASCII names correctly.
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_reads_back_cleanly(self, database, tmp_path):
        path = tmp_path / "inventory.csv"
        ExportService(database).to_file(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 4
        assert rows[0]["owner_email"] == "sai@worldbank.org"

    def test_suggested_filename_is_dated(self, database):
        name = ExportService(database).suggested_filename()
        assert name.startswith("connect-inventory-") and name.endswith(".csv")
