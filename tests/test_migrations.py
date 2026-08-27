"""Tests for the programmatic migration path.

The scenario that matters is the one that broke on Connect: tables already
created by `create_all()` before migrations existed, so no alembic_version row,
and a later revision adds a column. `create_all()` does NOT alter existing
tables, so it silently leaves the column missing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from database.models import Base


def columns(engine, table: str):
    return {c["name"] for c in inspect(engine).get_columns(table)}


class TestCreateAllLimitation:
    """Documents why create_all() was not enough - the bug this fixes."""

    def test_create_all_does_not_add_columns_to_existing_table(self):
        engine = create_engine("sqlite://")

        # Simulate the pre-migration schema: no owner_email column.
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE applications ("
                " id INTEGER PRIMARY KEY,"
                " content_guid VARCHAR(64) NOT NULL UNIQUE,"
                " app_name VARCHAR(512) NOT NULL,"
                " owner VARCHAR(256),"
                " content_url VARCHAR(1024),"
                " bundle_id VARCHAR(64),"
                " created_at DATETIME,"
                " updated_at DATETIME,"
                " last_inventory_scan DATETIME NOT NULL)"
            ))

        assert "owner_email" not in columns(engine, "applications")

        # The ORM model declares owner_email, but create_all() only creates
        # missing *tables* - it never issues ALTER TABLE.
        Base.metadata.create_all(engine)

        assert "owner_email" not in columns(engine, "applications"), (
            "create_all() must not be relied on to add columns to an existing table"
        )


class TestMigrationHelpers:
    def test_head_revision_is_latest(self):
        from database.migrations import head_revision

        assert head_revision() == "0003"

    def test_baseline_matches_first_revision(self):
        from database.migrations import BASELINE_REVISION
        from alembic.script import ScriptDirectory
        from config.settings import PROJECT_ROOT

        script = ScriptDirectory(str(PROJECT_ROOT / "alembic"))
        revisions = {r.revision for r in script.walk_revisions()}
        assert BASELINE_REVISION in revisions

    def test_revisions_form_a_chain(self):
        from alembic.script import ScriptDirectory
        from config.settings import PROJECT_ROOT

        script = ScriptDirectory(str(PROJECT_ROOT / "alembic"))
        revs = list(script.walk_revisions())
        assert [r.revision for r in revs] == ["0003", "0002", "0001"]
        assert revs[0].down_revision == "0002"
        assert revs[1].down_revision == "0001"
        assert revs[2].down_revision is None

    def test_migration_0002_adds_owner_email(self):
        """The migration must ALTER, which is what create_all() would not do."""
        import importlib.util
        from config.settings import PROJECT_ROOT

        path = PROJECT_ROOT / "alembic" / "versions" / "0002_add_owner_email.py"
        spec = importlib.util.spec_from_file_location("rev0002", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = path.read_text(encoding="utf-8")
        assert "add_column" in source
        assert "owner_email" in source
        assert module.revision == "0002"
        assert module.down_revision == "0001"

    def test_migration_0003_creates_findings_and_notifications(self):
        """Both tables plus the dedupe constraints must be created here, not
        left to create_all(), which never runs against the real database."""
        import importlib.util
        from config.settings import PROJECT_ROOT

        path = (
            PROJECT_ROOT
            / "alembic"
            / "versions"
            / "0003_add_findings_and_notifications.py"
        )
        spec = importlib.util.spec_from_file_location("rev0003", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = path.read_text(encoding="utf-8")
        assert "findings" in source
        assert "notifications" in source
        assert "uq_findings_identity" in source
        assert "uq_notifications_identity" in source
        assert module.revision == "0003"
        assert module.down_revision == "0002"


class TestOrmDeclaresColumn:
    def test_application_model_has_owner_email(self):
        from database.models import Application

        assert "owner_email" in Application.__table__.columns

    def test_fresh_create_all_includes_owner_email(self):
        """A brand-new database gets the column; only pre-existing ones need the migration."""
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        assert "owner_email" in columns(engine, "applications")
