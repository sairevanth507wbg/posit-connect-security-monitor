"""Application exceptions."""

from __future__ import annotations

from typing import Any, Optional, Tuple, Type


class InventoryError(Exception):
    pass


class ConfigurationError(InventoryError):
    pass


class ConnectAPIError(InventoryError):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        url: Optional[str] = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.payload = payload

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append("status=" + str(self.status_code))
        if self.url:
            parts.append("url=" + str(self.url))
        return " | ".join(parts)


class ConnectAuthError(ConnectAPIError):
    pass


class ConnectPermissionError(ConnectAPIError):
    pass


class ConnectNotFoundError(ConnectAPIError):
    pass


class ConnectRateLimitError(ConnectAPIError):
    pass


class ConnectServerError(ConnectAPIError):
    pass


class ConnectUnavailableError(ConnectAPIError):
    pass


# Only transient failures are retried. Retrying a 401 just fills Connect's
# audit log with failed authentications.
RETRYABLE_ERRORS: Tuple[Type[ConnectAPIError], ...] = (
    ConnectRateLimitError,
    ConnectServerError,
    ConnectUnavailableError,
)


class DatabaseError(InventoryError):
    pass


class DatabaseConnectionError(DatabaseError):
    pass


class MigrationError(DatabaseError):
    pass
