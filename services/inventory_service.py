"""Inventory scan orchestration."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from clients.connect_client import ConnectClient
from config.settings import Settings, get_settings
from database.connection import Database
from exceptions import ConnectAPIError, DatabaseError
from repositories.application_repository import ApplicationRepository
from repositories.package_repository import PackageRepository
from schemas.application import ApplicationSchema, UserSchema
from schemas.package import PackageSchema

logger = logging.getLogger(__name__)

# Above this many distinct owners, one bulk /v1/users call beats N lookups.
BULK_USER_THRESHOLD = 10

# The bool is whether the package fetch succeeded. A failed fetch must still
# record the application, but must not overwrite packages a previous scan stored.
FetchedApp = Tuple[ApplicationSchema, List[PackageSchema], bool]

ProgressCallback = Callable[[str, bool, int], None]


@dataclass
class ScanResult:
    started_at: datetime
    finished_at: Optional[datetime] = None
    discovered: int = 0
    applications_stored: int = 0
    packages_stored: int = 0
    applications_without_packages: int = 0
    applications_removed: int = 0
    skipped: int = 0
    failures: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def succeeded(self) -> bool:
        return not self.failures

    def as_dict(self) -> Dict[str, Any]:
        return {
            "discovered": self.discovered,
            "applications_stored": self.applications_stored,
            "packages_stored": self.packages_stored,
            "applications_without_packages": self.applications_without_packages,
            "applications_removed": self.applications_removed,
            "skipped": self.skipped,
            "failures": len(self.failures),
            "duration_seconds": round(self.duration_seconds, 2),
        }


class InventoryService:
    def __init__(
        self,
        client: ConnectClient,
        database: Database,
        settings: Optional[Settings] = None,
    ) -> None:
        self._client = client
        self._database = database
        self._settings = settings or get_settings()

    def run(
        self,
        *,
        limit: Optional[int] = None,
        guids: Optional[Sequence[str]] = None,
        collect_packages: bool = True,
        prune_removed: bool = False,
        progress: Optional[ProgressCallback] = None,
        on_discovered: Optional[Callable[[int], None]] = None,
    ) -> ScanResult:
        # One timestamp for the whole scan. Stale-package detection compares
        # against it, so it must not drift between chunks.
        scanned_at = datetime.now(timezone.utc)
        result = ScanResult(started_at=scanned_at)
        logger.info("Starting inventory scan")

        raw_items = self._discover_content(guids=guids)
        result.discovered = len(raw_items)
        if on_discovered is not None:
            on_discovered(result.discovered)

        if limit is not None and limit > 0:
            raw_items = raw_items[:limit]

        applications = self._parse_applications(raw_items, result)
        self._resolve_owners(applications)

        processed_guids: List[str] = []
        for chunk in self._chunks(applications, self._settings.batch_size):
            fetched = self._fetch_chunk(chunk, result, collect_packages=collect_packages)
            self._persist_chunk(
                fetched,
                result,
                processed_guids,
                scanned_at=scanned_at,
                replace_packages=collect_packages,
                progress=progress,
            )

        if prune_removed and guids is None and limit is None:
            result.applications_removed = self._prune(processed_guids)

        result.finished_at = datetime.now(timezone.utc)

        # A server without /v1/content/{guid}/packages 404s on every app, which
        # would otherwise look like a clean scan over an estate with no packages.
        if (
            collect_packages
            and result.applications_stored > 0
            and result.packages_stored == 0
        ):
            logger.warning(
                "Stored applications but ZERO packages. Either this Connect version "
                "does not expose /v1/content/{guid}/packages, or the API key cannot "
                "read it.",
                extra={"applications_stored": result.applications_stored},
            )

        logger.info("Inventory scan complete", extra=result.as_dict())
        return result

    def _discover_content(
        self, *, guids: Optional[Sequence[str]] = None
    ) -> List[Dict[str, Any]]:
        if guids:
            items: List[Dict[str, Any]] = []
            for guid in guids:
                try:
                    item = self._client.get_content(guid)
                    if item:
                        items.append(item)
                except ConnectAPIError as exc:
                    logger.error(
                        "Could not fetch content",
                        extra={"content_guid": guid, "error": str(exc)},
                    )
            return items
        return self._client.list_content()

    def _parse_applications(
        self, raw_items: Sequence[Dict[str, Any]], result: ScanResult
    ) -> List[ApplicationSchema]:
        applications: List[ApplicationSchema] = []
        server_url = self._settings.connect_server_url
        for item in raw_items:
            try:
                app = ApplicationSchema.model_validate(item)
            except ValidationError as exc:
                guid = str(item.get("guid", "<unknown>"))
                result.skipped += 1
                result.failures.append((guid, "invalid content payload: " + str(exc)))
                logger.warning(
                    "Skipping unparseable content record",
                    extra={"content_guid": guid, "error": str(exc)},
                )
                continue
            app.content_url = app.fallback_content_url(server_url)
            applications.append(app)
        return applications

    def _resolve_owners(self, applications: Sequence[ApplicationSchema]) -> None:
        # ?include=owner returns username and name but never email, so an
        # application still needs a user lookup whenever the email is missing.
        unresolved = {
            app.owner_guid
            for app in applications
            if app.owner_guid and not app.owner_email
        }
        if len(unresolved) > BULK_USER_THRESHOLD:
            users = self._client.list_users()
            if users:
                self._client.prime_user_cache(users)

        for app in applications:
            if app.owner_email:
                app.owner = app.resolved_owner()
                continue
            if not app.owner_guid:
                app.owner = app.resolved_owner()
                continue
            try:
                record = self._client.get_user(app.owner_guid)
            except ConnectAPIError:
                record = {}
            if record:
                try:
                    user = UserSchema.model_validate(record)
                    app.owner = user.display_name() or app.resolved_owner()
                    app.owner_email = app.owner_email or user.email
                    continue
                except ValidationError:
                    pass
            # Non-admin key or deleted user: keep the name ?include=owner gave
            # us rather than overwriting it with a raw GUID.
            app.owner = app.resolved_owner() or app.owner_guid

    def _fetch_chunk(
        self,
        chunk: Sequence[ApplicationSchema],
        result: ScanResult,
        *,
        collect_packages: bool,
    ) -> List[FetchedApp]:
        if not collect_packages:
            return [(app, [], False) for app in chunk]

        workers = min(self._settings.max_workers, len(chunk)) or 1
        fetched: List[FetchedApp] = []

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pkg-fetch") as pool:
            futures = [pool.submit(self._fetch_packages, app.content_guid) for app in chunk]
            # zip keeps the original order so console output stays deterministic.
            for app, future in zip(chunk, futures):
                try:
                    fetched.append((app, future.result(), True))
                except ConnectAPIError as exc:
                    result.failures.append((app.content_guid, str(exc)))
                    fetched.append((app, [], False))
                    logger.error(
                        "Failed to fetch packages; storing application without "
                        "touching its packages",
                        extra={
                            "content_guid": app.content_guid,
                            "app_name": app.app_name,
                            "error": str(exc),
                        },
                    )
                except Exception as exc:
                    result.failures.append((app.content_guid, "unexpected error: " + str(exc)))
                    fetched.append((app, [], False))
                    logger.exception(
                        "Unexpected error fetching packages",
                        extra={"content_guid": app.content_guid, "app_name": app.app_name},
                    )

        return fetched

    def _fetch_packages(self, content_guid: str) -> List[PackageSchema]:
        raw_packages = self._client.get_content_packages(content_guid)
        packages: List[PackageSchema] = []
        for entry in raw_packages:
            try:
                packages.append(PackageSchema.model_validate(entry))
            except ValidationError:
                continue
        return packages

    def _persist_chunk(
        self,
        fetched: Sequence[FetchedApp],
        result: ScanResult,
        processed_guids: List[str],
        *,
        scanned_at: datetime,
        replace_packages: bool,
        progress: Optional[ProgressCallback],
    ) -> None:
        if not fetched:
            return
        try:
            self._write_batch(
                fetched, result, processed_guids, scanned_at, replace_packages, progress
            )
        except DatabaseError as exc:
            # Retry one at a time so a single bad row can't discard the chunk.
            logger.warning(
                "Batch write failed; retrying individually",
                extra={"batch_size": len(fetched), "error": str(exc)},
            )
            for entry in fetched:
                try:
                    self._write_batch(
                        [entry], result, processed_guids, scanned_at, replace_packages, progress
                    )
                except DatabaseError as inner:
                    result.failures.append((entry[0].content_guid, str(inner)))
                    logger.error(
                        "Failed to store application",
                        extra={
                            "content_guid": entry[0].content_guid,
                            "app_name": entry[0].app_name,
                            "error": str(inner),
                        },
                    )
                    if progress is not None:
                        progress(entry[0].app_name, False, 0)

    def _write_batch(
        self,
        fetched: Sequence[FetchedApp],
        result: ScanResult,
        processed_guids: List[str],
        scanned_at: datetime,
        replace_packages: bool,
        progress: Optional[ProgressCallback],
    ) -> None:
        # Counted locally so a rollback can't leave the totals overstated.
        stored = 0
        package_total = 0
        without_packages = 0
        guids: List[str] = []
        reported: List[Tuple[str, bool, int]] = []

        with self._database.session() as session:
            app_repo = ApplicationRepository(session)
            package_repo = PackageRepository(session)

            app_repo.upsert_many([app for app, _, _ in fetched], scanned_at)

            for app, packages, packages_ok in fetched:
                if replace_packages and packages_ok:
                    count = package_repo.sync_for_content(
                        app.content_guid, packages, scanned_at
                    )
                    package_total += count
                    if count == 0:
                        without_packages += 1
                else:
                    # Leave what's already stored rather than replacing a good
                    # inventory with an empty one.
                    count = package_repo.count_for_content(app.content_guid)

                stored += 1
                guids.append(app.content_guid)
                reported.append((app.app_name, packages_ok or not replace_packages, count))

        result.applications_stored += stored
        result.packages_stored += package_total
        result.applications_without_packages += without_packages
        processed_guids.extend(guids)

        if progress is not None:
            for app_name, ok, count in reported:
                progress(app_name, ok, count)

    def _prune(self, live_guids: Sequence[str]) -> int:
        if not live_guids:
            logger.warning("Skipping prune: no applications were processed")
            return 0
        with self._database.session() as session:
            removed = ApplicationRepository(session).delete_missing(live_guids)
        if removed:
            logger.info("Pruned applications", extra={"removed": removed})
        return removed

    @staticmethod
    def _chunks(
        items: Sequence[ApplicationSchema], size: int
    ) -> Iterator[Sequence[ApplicationSchema]]:
        for start in range(0, len(items), size):
            yield items[start : start + size]
