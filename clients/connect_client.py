"""Posit Connect Server API client."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from config.settings import Settings, get_settings
from exceptions import (
    RETRYABLE_ERRORS,
    ConnectAPIError,
    ConnectAuthError,
    ConnectNotFoundError,
    ConnectPermissionError,
    ConnectRateLimitError,
    ConnectServerError,
    ConnectUnavailableError,
)

logger = logging.getLogger(__name__)

USER_AGENT = "connect-inventory-service/1.0"
MAX_ERROR_BODY_CHARS = 500
PAGINATION_GUARD = 10_000


class ConnectClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.api_base_url

        self._user_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

        # requests.Session isn't thread-safe, so each worker gets its own.
        self._local = threading.local()
        self._injected_session = session
        self._all_sessions: List[requests.Session] = []
        self._sessions_lock = threading.Lock()

    @property
    def _session(self) -> requests.Session:
        if self._injected_session is not None:
            return self._injected_session
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._build_session()
            self._local.session = session
            with self._sessions_lock:
                self._all_sessions.append(session)
        return session

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                # Connect uses "Key", not "Bearer".
                "Authorization": "Key " + self.settings.api_key_value(),
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
        )
        session.verify = self.settings.verify_ssl

        # tenacity handles retries; stacking urllib3's would multiply attempts.
        pool_size = max(10, self.settings.max_workers * 2)
        adapter = HTTPAdapter(
            max_retries=0, pool_connections=pool_size, pool_maxsize=pool_size
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        if not self.settings.verify_ssl:
            logger.warning("TLS verification is DISABLED")
        return session

    def close(self) -> None:
        if self._injected_session is not None:
            self._injected_session.close()
        with self._sessions_lock:
            for session in self._all_sessions:
                session.close()
            self._all_sessions.clear()

    def __enter__(self) -> "ConnectClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _log_retry(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        logger.warning(
            "Connect API call failed, retrying",
            extra={
                "attempt": state.attempt_number,
                "sleeping_for": round(state.next_action.sleep, 2) if state.next_action else None,
                "error": str(exc),
            },
        )

    def _retrying(self) -> Retrying:
        return Retrying(
            retry=retry_if_exception_type(RETRYABLE_ERRORS),
            wait=wait_exponential_jitter(
                initial=self.settings.retry_initial_wait,
                max=self.settings.retry_max_wait,
            ),
            stop=stop_after_attempt(self.settings.max_retries + 1),
            before_sleep=self._log_retry,
            reraise=True,
        )

    def _get(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._retrying()(self._get_once, path, params)

    def _get_once(self, path: str, params: Optional[Dict[str, Any]]) -> Any:
        url = self.base_url + path

        try:
            response = self._session.get(
                url, params=params, timeout=self.settings.request_timeout
            )
        except requests.exceptions.SSLError as exc:
            raise ConnectAPIError(
                "TLS verification failed for " + url
                + ". Install the CA bundle or set VERIFY_SSL=false.",
                url=url,
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ConnectUnavailableError(
                "Request timed out after " + str(self.settings.request_timeout) + "s", url=url
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ConnectUnavailableError(
                "Could not reach Posit Connect: " + str(exc), url=url
            ) from exc

        if response.status_code >= 400:
            self._raise_for_status(response, url)

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectAPIError(
                "Expected JSON but got " + str(response.headers.get("Content-Type"))
                + ". The URL probably points at the Connect web UI or a proxy "
                "login page rather than the API.",
                status_code=response.status_code,
                url=url,
            ) from exc

    @staticmethod
    def _raise_for_status(response: requests.Response, url: str) -> None:
        status = response.status_code
        body = (response.text or "").strip()[:MAX_ERROR_BODY_CHARS]

        try:
            payload = response.json()
            detail = payload.get("error") or payload.get("message") or body
        except ValueError:
            payload = None
            detail = body

        common = {"status_code": status, "url": url, "payload": payload}

        if status == 401:
            raise ConnectAuthError(
                "Authentication failed. CONNECT_API_KEY is invalid, expired, or "
                "belongs to a locked account.",
                **common,
            )
        if status == 403:
            raise ConnectPermissionError(
                "Permission denied. The API key lacks the required privilege "
                "(administrator is recommended).",
                **common,
            )
        if status == 404:
            raise ConnectNotFoundError("Resource not found: " + str(detail), **common)
        if status == 429:
            raise ConnectRateLimitError("Rate limited by Posit Connect.", **common)
        if status >= 500:
            raise ConnectServerError("Posit Connect server error: " + str(detail), **common)
        raise ConnectAPIError("Posit Connect API error: " + str(detail), **common)

    @staticmethod
    def _as_collection(payload: Any) -> List[Dict[str, Any]]:
        # Some endpoints return a bare array, paginated ones an envelope.
        if payload is None:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return [item for item in results if isinstance(item, dict)]
            return [payload]
        return []

    def _paginate(
        self, path: str, *, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        page_number = 1
        page_size = self.settings.page_size

        while True:
            request_params = dict(params or {})
            request_params.update({"page_number": page_number, "page_size": page_size})
            payload = self._get(path, params=request_params)
            batch = self._as_collection(payload)
            collected.extend(batch)

            if not isinstance(payload, dict) or "results" not in payload:
                break
            total = payload.get("total")
            if len(batch) < page_size:
                break
            if isinstance(total, int) and len(collected) >= total:
                break
            page_number += 1
            if page_number > PAGINATION_GUARD:
                logger.warning("Pagination guard triggered", extra={"path": path})
                break

        return collected

    def get_server_settings(self) -> Dict[str, Any]:
        payload = self._get("/server_settings")
        return payload if isinstance(payload, dict) else {}

    def whoami(self) -> Dict[str, Any]:
        payload = self._get("/v1/user")
        return payload if isinstance(payload, dict) else {}

    def verify_connection(self) -> Dict[str, Any]:
        server = self.get_server_settings()
        identity = self.whoami()
        info = {
            "version": server.get("version", "unknown"),
            "username": identity.get("username", "unknown"),
            "user_role": identity.get("user_role", "unknown"),
        }
        logger.info(
            "Connected to Posit Connect",
            extra={
                "connect_version": info["version"],
                "server": self.settings.connect_server_url,
                "username": info["username"],
                "user_role": info["user_role"],
            },
        )
        if info["user_role"] not in ("administrator", "unknown"):
            logger.warning(
                "API key is not an administrator; the inventory will only cover "
                "content this account can see.",
                extra={"user_role": info["user_role"]},
            )
        return info

    def list_content(self) -> List[Dict[str, Any]]:
        # ?include=owner avoids an N+1 user lookup, but older servers reject it.
        try:
            items = self._paginate("/v1/content", params={"include": "owner,tags"})
            logger.info("Discovered content", extra={"count": len(items)})
            return items
        except ConnectAPIError as exc:
            if exc.status_code not in (400, 404, 422):
                raise

        items = self._paginate("/v1/content")
        logger.info("Discovered content", extra={"count": len(items)})
        return items

    def get_content(self, content_guid: str) -> Dict[str, Any]:
        payload = self._get("/v1/content/" + quote(content_guid, safe=""))
        return payload if isinstance(payload, dict) else {}

    def get_content_packages(self, content_guid: str) -> List[Dict[str, Any]]:
        path = "/v1/content/" + quote(content_guid, safe="") + "/packages"
        try:
            return self._paginate(path)
        except ConnectNotFoundError:
            # No manifest recorded, e.g. static content.
            return []

    def list_users(self) -> List[Dict[str, Any]]:
        try:
            return self._paginate("/v1/users")
        except (ConnectPermissionError, ConnectNotFoundError):
            return []

    def get_user(self, user_guid: str) -> Dict[str, Any]:
        if not user_guid:
            return {}
        with self._cache_lock:
            if user_guid in self._user_cache:
                return self._user_cache[user_guid]

        try:
            payload = self._get("/v1/users/" + quote(user_guid, safe=""))
            record = payload if isinstance(payload, dict) else {}
        except (ConnectNotFoundError, ConnectPermissionError):
            # Non-admin keys can't read arbitrary users.
            record = {}

        with self._cache_lock:
            self._user_cache[user_guid] = record
        return record

    def prime_user_cache(self, users: Iterable[Dict[str, Any]]) -> None:
        with self._cache_lock:
            for user in users:
                guid = user.get("guid")
                if guid:
                    self._user_cache[guid] = user
