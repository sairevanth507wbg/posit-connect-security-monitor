"""Application configuration loaded from .env."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

from exceptions import ConfigurationError

logger = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
LOG_DIR: Path = PROJECT_ROOT / "logs"

VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
VALID_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
API_SUFFIXES = ("/__api__/v1", "/__api__")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_assignment=True,
    )

    connect_server_url: str
    connect_api_key: SecretStr

    postgres_host: str
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str
    postgres_user: str
    postgres_password: SecretStr
    postgres_sslmode: str = "prefer"
    postgres_sslrootcert: Optional[str] = None

    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=50)
    db_connect_timeout: int = Field(default=10, ge=1, le=300)
    db_echo: bool = False

    request_timeout: float = Field(default=30.0, gt=0, le=600)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_initial_wait: float = Field(default=0.5, gt=0, le=30)
    retry_max_wait: float = Field(default=10.0, gt=0, le=120)
    page_size: int = Field(default=500, ge=1, le=500)
    verify_ssl: bool = True

    max_workers: int = Field(default=8, ge=1, le=32)
    batch_size: int = Field(default=50, ge=1, le=1000)

    log_level: str = "INFO"
    log_json: bool = True
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(default=5, ge=0, le=50)
    ascii_output: bool = False

    @field_validator("connect_server_url", mode="before")
    @classmethod
    def _normalise_server_url(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        url = value.strip().rstrip("/")
        if not url:
            raise ValueError("CONNECT_SERVER_URL must not be empty")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("CONNECT_SERVER_URL must include https://")
        for suffix in API_SUFFIXES:
            if url.lower().endswith(suffix):
                return url[: -len(suffix)].rstrip("/")
        return url

    @field_validator("connect_api_key", "postgres_password", mode="before")
    @classmethod
    def _require_secret(cls, value: Any) -> Any:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("must not be empty")
        return raw.strip()

    @field_validator("postgres_host", "postgres_db", "postgres_user", mode="before")
    @classmethod
    def _require_value(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("postgres_sslmode", mode="before")
    @classmethod
    def _check_sslmode(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        mode = value.strip().lower()
        if mode not in VALID_SSLMODES:
            raise ValueError("POSTGRES_SSLMODE must be one of " + str(sorted(VALID_SSLMODES)))
        return mode

    @field_validator("log_level", mode="before")
    @classmethod
    def _check_log_level(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        level = value.strip().upper()
        if level not in VALID_LOG_LEVELS:
            raise ValueError("LOG_LEVEL must be one of " + str(sorted(VALID_LOG_LEVELS)))
        return level

    @property
    def api_base_url(self) -> str:
        return self.connect_server_url + "/__api__"

    @property
    def database_url(self) -> URL:
        query = {"sslmode": self.postgres_sslmode}
        if self.postgres_sslrootcert:
            query["sslrootcert"] = self.postgres_sslrootcert
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
            query=query,
        )

    def database_url_string(self, *, hide_password: bool = True) -> str:
        return self.database_url.render_as_string(hide_password=hide_password)

    @property
    def log_dir(self) -> Path:
        return LOG_DIR

    def api_key_value(self) -> str:
        return self.connect_api_key.get_secret_value()

    def safe_summary(self) -> Dict[str, Any]:
        return {
            "connect_server_url": self.connect_server_url,
            "connect_api_key": "***redacted***",
            "database": self.database_url_string(),
            "postgres_sslmode": self.postgres_sslmode,
            "request_timeout": self.request_timeout,
            "max_retries": self.max_retries,
            "max_workers": self.max_workers,
            "batch_size": self.batch_size,
            "log_level": self.log_level,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        details = "; ".join(
            (".".join(str(p) for p in err["loc"]).upper() or "<root>") + ": " + err["msg"]
            for err in exc.errors()
        )
        raise ConfigurationError(
            "Invalid configuration. Check " + str(PROJECT_ROOT / ".env") + ". " + details
        ) from exc
