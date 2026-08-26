"""Logging setup: readable console on stderr, JSON lines to file."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_CONFIGURED = False

CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
NOISY_LIBRARIES = ("urllib3", "requests", "sqlalchemy.engine.Engine", "alembic")

# Everything else on a LogRecord came from extra= and belongs in the payload.
RESERVED_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        if record.threadName and record.threadName != "MainThread":
            payload["thread"] = record.threadName

        for key, value in record.__dict__.items():
            if key not in RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    *,
    json_file: bool = True,
    log_file_name: str = "inventory.log",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    force: bool = False,
) -> logging.Logger:
    global _CONFIGURED

    root = logging.getLogger()
    if _CONFIGURED and not force:
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    console_level = getattr(logging, str(level).upper(), logging.INFO)
    # Root must pass DEBUG through when the file sink wants it; each handler
    # applies its own threshold.
    root.setLevel(logging.DEBUG if log_dir is not None else console_level)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(console)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                filename=log_dir / log_file_name,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                JsonFormatter()
                if json_file
                else logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    datefmt=DATE_FORMAT,
                )
            )
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("File logging disabled (%s): %s", log_dir, exc)

    for noisy in NOISY_LIBRARIES:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return root
