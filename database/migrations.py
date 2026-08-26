"""Programmatic Alembic migrations.

The database is reachable only from inside the VNet, so `alembic upgrade head`
cannot be run from a workstation. This lets the deployed content run its own
migrations at startup instead.

Adopting an existing schema
---------------------------
Earlier runs created the tables with `Base.metadata.create_all()`, which leaves
no `alembic_version` row. Running `upgrade head` against that would try to
re-create existing tables and fail, so the schema is stamped at the revision
that matches what is already there, then upgraded normally.
"""

from __future__ import annotations

import logging
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from config.settings import PROJECT_ROOT
from exceptions import MigrationError

logger = logging.getLogger(__name__)

# Revision that matches a schema created by create_all() before migrations
# were wired in: applications + packages, without owner_email.
BASELINE_REVISION = "0001"


def _config(database_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    # ConfigParser treats % as interpolation; escape it for encoded passwords.
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    cfg.attributes["programmatic"] = True
    return cfg


def current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> Optional[str]:
    script = ScriptDirectory(str(PROJECT_ROOT / "alembic"))
    return script.get_current_head()


def ensure_schema(engine: Engine, database_url: str) -> str:
    """Bring the database up to the latest revision. Safe to run every time.

    Returns the revision the database is on afterwards.
    """
    try:
        tables = set(inspect(engine).get_table_names())
        cfg = _config(database_url)

        if "alembic_version" not in tables and "applications" in tables:
            logger.info(
                "Existing schema has no migration history; stamping baseline",
                extra={"revision": BASELINE_REVISION},
            )
            command.stamp(cfg, BASELINE_REVISION)

        before = current_revision(engine)
        command.upgrade(cfg, "head")
        after = current_revision(engine)

        if before == after:
            logger.info("Schema already current", extra={"revision": after})
        else:
            logger.info(
                "Schema migrated", extra={"from_revision": before, "to_revision": after}
            )
        return after or "unknown"

    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError("Migration failed: " + str(exc)) from exc
