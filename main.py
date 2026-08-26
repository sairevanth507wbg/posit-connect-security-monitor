"""Entry point.

    python main.py                  full scan
    python main.py --check          verify Connect + PostgreSQL, then exit
    python main.py --limit 5 -v     smoke test five applications
    python main.py --db-stats       show what is stored, no network calls
    python main.py --export-csv inventory.csv   write the inventory as CSV
    python main.py --export-zip inventory.zip   write the same CSV, zipped

Exit codes: 0 ok, 1 completed with failures, 2 fatal.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

from clients.connect_client import ConnectClient
from config.logging_config import configure_logging
from config.settings import PROJECT_ROOT, Settings, get_settings
from database.connection import Database, get_database
from exceptions import (
    ConfigurationError,
    DatabaseConnectionError,
    InventoryError,
    MigrationError,
)
from repositories.application_repository import ApplicationRepository
from repositories.package_repository import PackageRepository
from services.export_service import ExportService
from services.inventory_service import InventoryService, ScanResult

logger = logging.getLogger("connect_inventory")

EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_FATAL = 2


class Console:
    """Writes the scan report to stdout.

    Windows consoles default to cp1252, which can't encode U+2713 - printing it
    raw raises UnicodeEncodeError and kills the run. Switch the stream to UTF-8
    where possible, fall back to ASCII where not.
    """

    UNICODE_OK = "✓"
    UNICODE_FAIL = "✗"
    ASCII_OK = "[OK]"
    ASCII_FAIL = "[!!]"

    def __init__(self, stream=None, *, force_ascii: bool = False) -> None:
        self._stream = stream or sys.stdout
        self._unicode = not force_ascii and self._enable_unicode()

    def _enable_unicode(self) -> bool:
        if self._can_encode(self.UNICODE_OK):
            return True
        reconfigure = getattr(self._stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                return False
            return self._can_encode(self.UNICODE_OK)
        return False

    def _can_encode(self, text: str) -> bool:
        encoding = getattr(self._stream, "encoding", None)
        if not encoding:
            return False
        try:
            text.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return False
        return True

    @property
    def ok_mark(self) -> str:
        return self.UNICODE_OK if self._unicode else self.ASCII_OK

    @property
    def fail_mark(self) -> str:
        return self.UNICODE_FAIL if self._unicode else self.ASCII_FAIL

    def line(self, text: str = "") -> None:
        print(text, file=self._stream, flush=True)

    def scan_started(self) -> None:
        self.line("Starting inventory scan...")
        self.line()

    def discovered(self, count: int) -> None:
        self.line("Found " + str(count) + " deployed applications.")
        self.line()
        if count:
            self.line("Processing:")

    def application_done(self, app_name: str, ok: bool, package_count: int) -> None:
        mark = self.ok_mark if ok else self.fail_mark
        suffix = "" if ok else "  (packages unavailable)"
        self.line(mark + " " + app_name + suffix)

    def scan_complete(self, result: ScanResult) -> None:
        self.line()
        self.line("Inventory Scan Complete")
        self.line()
        self.line("Applications Stored: " + str(result.applications_stored))
        self.line("Packages Stored: " + str(result.packages_stored))
        if result.failures:
            self.line()
            self.line(
                "Completed with " + str(len(result.failures))
                + " failure(s) - see logs/inventory.log"
            )
            for guid, message in result.failures[:10]:
                self.line("  - " + guid + ": " + message[:150])
            if len(result.failures) > 10:
                self.line("  ... and " + str(len(result.failures) - 10) + " more")
        self.line()


def print_db_stats(database: Database, settings: Settings, console: Console) -> None:
    console.line()
    console.line("=" * 58)
    console.line("DATABASE STATUS")
    console.line("=" * 58)
    console.line("Server : " + settings.database_url_string())

    with database.session() as session:
        app_repo = ApplicationRepository(session)
        package_repo = PackageRepository(session)

        console.line()
        console.line("Rows stored")
        console.line("  applications        : " + str(app_repo.count()))
        console.line("  packages            : " + str(package_repo.count()))
        console.line("  distinct owners     : " + str(app_repo.distinct_owner_count()))
        console.line(
            "  unique pkg versions : " + str(package_repo.distinct_package_versions())
        )

        breakdown = package_repo.package_type_breakdown()
        if breakdown:
            parts = [name + "=" + str(count) for name, count in sorted(breakdown.items())]
            console.line("  by runtime          : " + ", ".join(parts))

        oldest, newest = app_repo.scan_time_range()
        if newest is not None:
            console.line()
            console.line("Scans")
            console.line("  oldest row scanned  : " + _format_ts(oldest))
            console.line("  newest row scanned  : " + _format_ts(newest))
        else:
            console.line()
            console.line("Schema exists but no applications are stored yet.")
            console.line("Run:  python main.py")
    console.line()


def _format_ts(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip() if value else "(none)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connect-inventory-service",
        description="Discover deployed Posit Connect content and store its "
                    "package inventory in PostgreSQL.",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="process at most N applications")
    parser.add_argument("--guid", action="append", dest="guids", default=None,
                        metavar="GUID", help="scan only this content GUID (repeatable)")
    parser.add_argument("--no-packages", action="store_true",
                        help="record applications only")
    parser.add_argument("--prune", action="store_true",
                        help="delete stored applications Connect no longer reports")
    parser.add_argument("--workers", type=int, default=None, metavar="N",
                        help="concurrent package-fetch threads (default 8)")
    parser.add_argument("--check", action="store_true",
                        help="verify config, Connect, and PostgreSQL, then exit")
    parser.add_argument("--export-csv", dest="export_csv", default=None, metavar="PATH",
                        help="write the inventory to a CSV file and exit")
    parser.add_argument("--export-zip", dest="export_zip", default=None, metavar="PATH",
                        help="write the inventory as a zipped CSV and exit")
    parser.add_argument("--export-applications", action="store_true",
                        dest="export_applications",
                        help="with --export-csv/--export-zip, one row per application")
    parser.add_argument("--db-stats", action="store_true", dest="db_stats",
                        help="show what is stored and exit")
    parser.add_argument("--create-tables", action="store_true", dest="create_tables",
                        help="create tables from ORM metadata (prefer alembic)")
    parser.add_argument("--ascii", action="store_true",
                        help="force ASCII console markers")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings only")
    return parser


def _resolve_log_level(args: argparse.Namespace, settings: Settings) -> str:
    if args.verbose:
        return "DEBUG"
    if args.quiet:
        return "WARNING"
    return settings.log_level


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Config loads before logging so a bad .env fails early and loudly.
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        configure_logging("INFO", PROJECT_ROOT / "logs")
        logger.error("%s", exc)
        print("Configuration error: " + str(exc), file=sys.stderr)
        print("Copy .env.example to .env and fill in the values.", file=sys.stderr)
        return EXIT_FATAL

    configure_logging(
        level=_resolve_log_level(args, settings),
        log_dir=settings.log_dir,
        json_file=settings.log_json,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        force=True,
    )

    if args.workers is not None:
        if not 1 <= args.workers <= 32:
            print("--workers must be between 1 and 32", file=sys.stderr)
            return EXIT_FATAL
        settings.max_workers = args.workers

    console = Console(force_ascii=args.ascii or settings.ascii_output)

    database: Optional[Database] = None
    try:
        database = get_database(settings)
        database.verify_connection()

        if args.create_tables:
            database.create_all()
            print("Schema created.", file=sys.stderr)

        if args.export_csv:
            database.verify_schema()
            rows = ExportService(database).to_file(
                Path(args.export_csv), applications_only=args.export_applications
            )
            print("Wrote " + str(rows) + " row(s) to " + args.export_csv, file=sys.stderr)
            return EXIT_SUCCESS

        if args.export_zip:
            database.verify_schema()
            export = ExportService(database)
            rows = export.to_zip_file(
                Path(args.export_zip),
                applications_only=args.export_applications,
                arcname=export.suggested_filename(),
            )
            print("Wrote " + str(rows) + " row(s) to " + args.export_zip, file=sys.stderr)
            return EXIT_SUCCESS

        if args.db_stats:
            database.verify_schema()
            print_db_stats(database, settings, console)
            return EXIT_SUCCESS

        database.verify_schema()

        with ConnectClient(settings) as client:
            info = client.verify_connection()
            print(
                "Connected to Posit Connect " + str(info["version"])
                + " as " + str(info["username"])
                + " (role=" + str(info["user_role"]) + ")",
                file=sys.stderr,
            )
            if args.check:
                print("Configuration, Connect, and PostgreSQL all OK.", file=sys.stderr)
                return EXIT_SUCCESS

            console.scan_started()
            service = InventoryService(client, database, settings)
            result = service.run(
                limit=args.limit,
                guids=args.guids,
                collect_packages=not args.no_packages,
                prune_removed=args.prune,
                progress=console.application_done,
                on_discovered=console.discovered,
            )

        console.scan_complete(result)
        return EXIT_SUCCESS if result.succeeded else EXIT_PARTIAL_FAILURE

    except MigrationError as exc:
        logger.error("%s", exc)
        print("Schema error: " + str(exc), file=sys.stderr)
        return EXIT_FATAL
    except DatabaseConnectionError as exc:
        logger.error("%s", exc)
        print("Database error: " + str(exc), file=sys.stderr)
        return EXIT_FATAL
    except InventoryError as exc:
        logger.error("Fatal error: %s", exc)
        print("Error: " + str(exc), file=sys.stderr)
        return EXIT_FATAL
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return EXIT_FATAL
    except Exception as exc:
        logger.exception("Unhandled error")
        print("Unexpected error: " + str(exc), file=sys.stderr)
        return EXIT_FATAL
    finally:
        if database is not None:
            database.dispose()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
