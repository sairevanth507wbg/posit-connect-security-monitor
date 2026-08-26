"""Tests for the scan orchestration and the console report.

The database is replaced with fakes so the full flow - discovery, owner
resolution, concurrent package fetching, ordered progress, and persistence
decisions - can be exercised without PostgreSQL.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
from typing import Any, Dict, List, Sequence, Tuple

import pytest

import services.inventory_service as service_module
from clients.connect_client import ConnectClient
from main import Console
from services.inventory_service import InventoryService, ScanResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class EncodedStringIO(io.StringIO):
    """StringIO with a readable ``encoding``, which StringIO lacks."""

    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding


class FakeSession:
    pass


class FakeDatabase:
    """Stands in for :class:`database.connection.Database`."""

    def __init__(self) -> None:
        self.commits = 0

    @contextmanager
    def session(self):
        yield FakeSession()
        self.commits += 1


class FakeAppRepo:
    upserted: List[Tuple[str, Any]] = []

    def __init__(self, session):
        pass

    def upsert_many(self, applications, scanned_at) -> int:
        for app in applications:
            FakeAppRepo.upserted.append((app.content_guid, scanned_at))
        return len(applications)

    def delete_missing(self, live_guids) -> int:
        return 0


class FakePackageRepo:
    synced: List[Tuple[str, int, Any]] = []
    counted: List[str] = []

    def __init__(self, session):
        pass

    def sync_for_content(self, content_guid, packages, scanned_at) -> int:
        FakePackageRepo.synced.append((content_guid, len(packages), scanned_at))
        return len(packages)

    def count_for_content(self, content_guid) -> int:
        FakePackageRepo.counted.append(content_guid)
        return 99  # a previously stored inventory


@pytest.fixture
def fake_repos(monkeypatch):
    FakeAppRepo.upserted = []
    FakePackageRepo.synced = []
    FakePackageRepo.counted = []
    monkeypatch.setattr(service_module, "ApplicationRepository", FakeAppRepo)
    monkeypatch.setattr(service_module, "PackageRepository", FakePackageRepo)
    return FakeAppRepo, FakePackageRepo


@pytest.fixture
def service(settings, fake_repos):
    client = ConnectClient(settings)
    database = FakeDatabase()
    yield InventoryService(client, database, settings), database
    client.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class TestScan:
    def test_discovers_and_stores_all_applications(self, service):
        svc, _ = service
        result = svc.run()
        assert result.discovered == 3
        assert result.applications_stored == 3
        assert result.succeeded

    def test_all_packages_passed_to_repository(self, service, fake_repos):
        """The service forwards what Connect returned; the repository dedupes."""
        svc, _ = service
        svc.run()
        _, package_repo = fake_repos
        by_guid = {guid: count for guid, count, _ in package_repo.synced}
        assert by_guid["c-treasury"] == 4  # includes the duplicate pandas entry
        assert by_guid["c-risk"] == 3
        assert by_guid["c-static"] == 0

    def test_owner_resolved_to_display_name(self, service):
        svc, _ = service
        applications = svc._parse_applications(svc._discover_content(), ScanResult(
            started_at=__import__("datetime").datetime.now()
        ))
        svc._resolve_owners(applications)
        owners = {app.content_guid: app.owner for app in applications}
        assert owners["c-treasury"] == "Sai Revanth"
        assert owners["c-risk"] == "Jane Doe"

    def test_single_scanned_at_used_throughout(self, service, fake_repos):
        """Stale-package detection breaks if the timestamp drifts mid-scan."""
        svc, _ = service
        svc.run()
        app_repo, package_repo = fake_repos
        stamps = {ts for _, ts in app_repo.upserted}
        stamps |= {ts for _, _, ts in package_repo.synced}
        assert len(stamps) == 1

    def test_limit_restricts_work(self, service):
        svc, _ = service
        result = svc.run(limit=1)
        assert result.discovered == 3
        assert result.applications_stored == 1

    def test_no_packages_flag_skips_package_sync(self, service, fake_repos):
        svc, _ = service
        result = svc.run(collect_packages=False)
        assert result.applications_stored == 3
        assert fake_repos[1].synced == []
        assert result.packages_stored == 0


class TestFailureHandling:
    def test_failed_package_fetch_still_stores_application(
        self, service, connect_state, fake_repos
    ):
        """A transient packages failure must not remove an app from inventory."""
        connect_state.fail_status = 500
        connect_state.fail_times["/content/c-treasury/packages"] = 99

        svc, _ = service
        result = svc.run()

        # The application is still recorded...
        assert "c-treasury" in [guid for guid, _ in fake_repos[0].upserted]
        assert result.applications_stored == 3
        # ...but its packages were NOT replaced with an empty set.
        synced_guids = [guid for guid, _, _ in fake_repos[1].synced]
        assert "c-treasury" not in synced_guids
        assert "c-treasury" in fake_repos[1].counted
        # ...and the failure is reported.
        assert len(result.failures) == 1
        assert not result.succeeded

    def test_partial_failure_does_not_stop_other_applications(
        self, service, connect_state, fake_repos
    ):
        connect_state.fail_status = 500
        connect_state.fail_times["/content/c-risk/packages"] = 99
        svc, _ = service
        result = svc.run()
        synced_guids = [guid for guid, _, _ in fake_repos[1].synced]
        assert "c-treasury" in synced_guids and "c-static" in synced_guids
        assert result.applications_stored == 3


class TestProgressOrdering:
    def test_progress_is_deterministic_despite_concurrency(self, service):
        """Concurrent fetching must not scramble the console output order."""
        svc, _ = service
        seen: List[str] = []
        svc.run(progress=lambda name, ok, count: seen.append(name))
        assert seen == ["Treasury Dashboard", "Risk Analytics", "Static Docs"]

    def test_progress_reports_failure_flag(self, service, connect_state):
        connect_state.fail_status = 500
        connect_state.fail_times["/content/c-risk/packages"] = 99
        svc, _ = service
        seen: List[Tuple[str, bool]] = []
        svc.run(progress=lambda name, ok, count: seen.append((name, ok)))
        assert dict(seen)["Risk Analytics"] is False
        assert dict(seen)["Treasury Dashboard"] is True


class TestPackageDeduplication:
    """The real repository collapses duplicates before building the INSERT.

    PostgreSQL raises "ON CONFLICT DO UPDATE command cannot affect row a second
    time" if one statement contains two rows with the same conflict key, so this
    is a correctness requirement, not an optimisation.
    """

    def test_dedupe_collapses_identical_entries(self):
        from repositories.package_repository import PackageRepository
        from schemas.package import PackageSchema

        packages = [
            PackageSchema.model_validate({"name": "pandas", "version": "2.2.2",
                                          "language": "python"}),
            PackageSchema.model_validate({"name": "pandas", "version": "2.2.2",
                                          "language": "python"}),
            PackageSchema.model_validate({"name": "numpy", "version": "2.1.0",
                                          "language": "python"}),
        ]
        deduped = PackageRepository._dedupe(packages)
        assert len(deduped) == 2
        assert ("pandas", "2.2.2", "Python") in deduped

    def test_dedupe_keeps_distinct_versions(self):
        from repositories.package_repository import PackageRepository
        from schemas.package import PackageSchema

        packages = [
            PackageSchema.model_validate({"name": "pandas", "version": "2.2.2",
                                          "language": "python"}),
            PackageSchema.model_validate({"name": "pandas", "version": "2.1.0",
                                          "language": "python"}),
        ]
        assert len(PackageRepository._dedupe(packages)) == 2

    def test_dedupe_drops_nameless_entries(self):
        from repositories.package_repository import PackageRepository

        class Blank:
            package_name = ""
            identity = ("", "", "")

        assert PackageRepository._dedupe([Blank()]) == {}


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------


class TestConsoleOutput:
    def _render(self, result: ScanResult, apps: Sequence[Tuple[str, bool, int]]) -> str:
        stream = EncodedStringIO("utf-8")
        console = Console(stream=stream)
        console.scan_started()
        console.discovered(result.discovered)
        for name, ok, count in apps:
            console.application_done(name, ok, count)
        console.scan_complete(result)
        return stream.getvalue()

    def test_matches_specified_format(self):
        result = ScanResult(
            started_at=__import__("datetime").datetime.now(),
            finished_at=__import__("datetime").datetime.now(),
            discovered=145,
            applications_stored=145,
            packages_stored=3248,
        )
        output = self._render(
            result,
            [("Treasury Dashboard", True, 12), ("Risk Analytics", True, 18),
             ("Customer Insights", True, 9)],
        )
        assert "Starting inventory scan..." in output
        assert "Found 145 deployed applications." in output
        assert "Processing:" in output
        assert "✓ Treasury Dashboard" in output
        assert "✓ Risk Analytics" in output
        assert "✓ Customer Insights" in output
        assert "Inventory Scan Complete" in output
        assert "Applications Stored: 145" in output
        assert "Packages Stored: 3248" in output

    def test_ascii_fallback_when_console_cannot_encode(self):
        stream = EncodedStringIO("cp1252")
        console = Console(stream=stream)
        console.application_done("Treasury Dashboard", True, 12)
        assert "[OK] Treasury Dashboard" in stream.getvalue()

    def test_failure_marker_and_summary(self):
        result = ScanResult(
            started_at=__import__("datetime").datetime.now(),
            finished_at=__import__("datetime").datetime.now(),
            discovered=2, applications_stored=2, packages_stored=5,
            failures=[("c-risk", "boom")],
        )
        output = self._render(result, [("Risk Analytics", False, 0)])
        assert "✗ Risk Analytics  (packages unavailable)" in output
        assert "Completed with 1 failure(s)" in output
