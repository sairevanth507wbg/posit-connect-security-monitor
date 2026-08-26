"""Database engine and sessions."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from config.settings import Settings, get_settings
from database.models import Application, Base, Package
from exceptions import DatabaseConnectionError, DatabaseError, MigrationError

logger = logging.getLogger(__name__)

REQUIRED_TABLES = ("applications", "packages")


class Database:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self.engine: Engine = create_engine(
            self._settings.database_url,
            echo=self._settings.db_echo,
            future=True,
            pool_pre_ping=True,
            pool_size=self._settings.db_pool_size,
            max_overflow=self._settings.db_max_overflow,
            pool_recycle=1800,
            # Without this an unreachable host hangs on the OS default rather
            # than failing in seconds.
            connect_args={"connect_timeout": self._settings.db_connect_timeout},
        )
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    def verify_connection(self) -> str:
        try:
            with self.engine.connect() as conn:
                version = conn.execute(text("SELECT version()")).scalar_one()
        except OperationalError as exc:
            raise DatabaseConnectionError(
                "Could not connect to PostgreSQL at "
                + self._settings.database_url_string()
                + ". Check POSTGRES_HOST/PORT/DB/USER/PASSWORD and that the "
                "server accepts connections from this host. Cause: "
                + str(exc.orig or exc)
            ) from exc
        except SQLAlchemyError as exc:
            raise DatabaseConnectionError("PostgreSQL connection failed: " + str(exc)) from exc

        logger.info("Connected to PostgreSQL", extra={"server_version": str(version)})
        return str(version)

    def verify_schema(self) -> None:
        """Check tables *and* columns.

        Checking only tables let a stale schema through: the tables existed but
        a later migration's column did not, so the failure surfaced much later
        as an opaque UndefinedColumn deep inside an unrelated query.
        """
        inspector = inspect(self.engine)
        existing = set(inspector.get_table_names())

        missing_tables = [t for t in REQUIRED_TABLES if t not in existing]
        if missing_tables:
            raise MigrationError(
                "Missing table(s): " + ", ".join(missing_tables)
                + ". The schema has not been created. Run: alembic upgrade head"
            )

        problems = []
        for table, model in (("applications", Application), ("packages", Package)):
            expected = {c.name for c in model.__table__.columns}
            actual = {c["name"] for c in inspector.get_columns(table)}
            absent = sorted(expected - actual)
            if absent:
                problems.append(table + ": " + ", ".join(absent))

        if problems:
            raise MigrationError(
                "Schema is out of date - missing column(s): "
                + "; ".join(problems)
                + ". Migrations did not apply. Confirm Connect is running the "
                "latest bundle, then re-run. From a host with database access: "
                "alembic upgrade head"
            )

    def ensure_schema(self) -> str:
        """Run any outstanding migrations. Safe to call on every run."""
        from database.migrations import ensure_schema

        return ensure_schema(
            self.engine, self._settings.database_url_string(hide_password=False)
        )

    def create_all(self) -> None:
        try:
            Base.metadata.create_all(self.engine)
            logger.info("Schema created from ORM metadata")
        except SQLAlchemyError as exc:
            raise DatabaseError("Failed to create schema: " + str(exc)) from exc

    def drop_all(self) -> None:
        try:
            Base.metadata.drop_all(self.engine)
            logger.warning("Schema dropped")
        except SQLAlchemyError as exc:
            raise DatabaseError("Failed to drop schema: " + str(exc)) from exc

    @contextmanager
    def session(self) -> Iterator[Session]:
        db_session = self.session_factory()
        try:
            yield db_session
            db_session.commit()
        except SQLAlchemyError as exc:
            db_session.rollback()
            logger.exception("Transaction rolled back")
            raise DatabaseError("Database operation failed: " + str(exc)) from exc
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.close()

    def dispose(self) -> None:
        self.engine.dispose()


_database: Optional[Database] = None


def get_database(settings: Optional[Settings] = None) -> Database:
    global _database
    if _database is None:
        _database = Database(settings)
    return _database


def reset_database_singleton() -> None:
    global _database
    if _database is not None:
        _database.dispose()
    _database = None
