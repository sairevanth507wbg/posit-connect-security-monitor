"""Alembic environment.

Reads the database URL from the application's own settings (``.env``) rather
than from ``alembic.ini``, so credentials are configured in exactly one place
and never committed.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project importable when Alembic runs from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings  # noqa: E402
from database.models import Base  # noqa: E402

config = context.config

# Only configure logging from alembic.ini when run via the CLI. When called
# programmatically the application has already set up logging, and fileConfig
# would tear those handlers down.
if config.config_file_name is not None and not config.attributes.get("programmatic"):
    fileConfig(config.config_file_name)

# Autogenerate compares the live database against this metadata.
target_metadata = Base.metadata

# A programmatic caller (database.migrations) sets the URL itself. Only fall
# back to .env when running the alembic CLI, so the caller's URL is not
# silently overwritten - and so this file does not require a .env to import.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option(
        "sqlalchemy.url",
        # Escape % so ConfigParser interpolation does not choke on passwords.
        get_settings().database_url_string(hide_password=False).replace("%", "%%"),
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
