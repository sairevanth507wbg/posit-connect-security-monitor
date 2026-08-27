"""Wiz API client: authentication now, scanning once OIS confirms the shape.

Authentication is OAuth2 client credentials, which Wiz documents publicly and
which this module implements in full. The moment OIS issues credentials you can
run verify_connection() and prove they work, before anything else is built.

Submitting an SBOM and reading findings back are deliberately NOT implemented.
Wiz exposes those through a GraphQL API whose request shape depends on the
tenant, and inventing endpoints here would produce code that fails against the
real service while looking finished. Both raise an error naming exactly what
OIS still has to supply. Filling them in changes nothing elsewhere.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import Settings
from exceptions import WizAPIError, WizAuthError, WizNotConfiguredError

logger = logging.getLogger(__name__)

# Refresh a little before the token actually expires so a long scan does not
# fail on a token that lapsed mid-request.
TOKEN_LEEWAY_SECONDS = 60


class WizClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.wiz_configured:
            raise WizNotConfiguredError(
                "Wiz is not configured. Set WIZ_API_URL, WIZ_CLIENT_ID and "
                "WIZ_CLIENT_SECRET in .env (or the Vars panel on Connect). "
                "Ask OIS for service account credentials."
            )
        self.settings = settings
        self._session_obj: Optional[requests.Session] = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ---- plumbing -------------------------------------------------------

    @property
    def _session(self) -> requests.Session:
        if self._session_obj is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
            session.verify = self.settings.verify_ssl
            self._session_obj = session
        return self._session_obj

    def close(self) -> None:
        if self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    def __enter__(self) -> "WizClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _log_retry(state: RetryCallState) -> None:
        logger.warning(
            "Retrying Wiz request",
            extra={"attempt": state.attempt_number},
        )

    def _retrying(self) -> Retrying:
        return Retrying(
            stop=stop_after_attempt(self.settings.max_retries + 1),
            wait=wait_exponential(
                multiplier=self.settings.retry_initial_wait,
                max=self.settings.retry_max_wait,
            ),
            retry=retry_if_exception_type(WizAPIError),
            before_sleep=self._log_retry,
            reraise=True,
        )

    # ---- authentication -------------------------------------------------

    def _fetch_token(self) -> str:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.settings.wiz_client_id,
            "client_secret": self.settings.wiz_client_secret_value(),
            "audience": self.settings.wiz_audience,
        }
        try:
            response = self._session.post(
                self.settings.wiz_auth_url,
                data=payload,
                timeout=self.settings.request_timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except requests.RequestException as exc:
            raise WizAPIError("Could not reach the Wiz auth endpoint: " + str(exc)) from exc

        if response.status_code in (400, 401, 403):
            # Wrong credentials are not worth retrying.
            raise WizAuthError(
                "Wiz rejected the credentials (HTTP "
                + str(response.status_code)
                + "). Check WIZ_CLIENT_ID, WIZ_CLIENT_SECRET and WIZ_AUDIENCE."
            )
        if response.status_code >= 400:
            raise WizAPIError("Wiz auth failed with HTTP " + str(response.status_code))

        try:
            body = response.json()
        except ValueError as exc:
            raise WizAPIError("Wiz auth returned a non-JSON response") from exc

        access_token = body.get("access_token")
        if not access_token:
            raise WizAuthError("Wiz auth succeeded but returned no access_token")

        expires_in = float(body.get("expires_in") or 3600)
        self._token_expires_at = time.monotonic() + expires_in - TOKEN_LEEWAY_SECONDS
        logger.info("Obtained Wiz access token", extra={"expires_in": expires_in})
        return str(access_token)

    def token(self) -> str:
        """Return a valid access token, fetching or refreshing as needed."""
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        self._token = self._retrying()(self._fetch_token)
        return self._token

    def verify_connection(self) -> Dict[str, Any]:
        """Prove the credentials and URLs are correct. Safe to call anytime."""
        self.token()
        return {
            "api_url": self.settings.wiz_api_url,
            "authenticated": True,
        }

    # ---- scanning: awaiting the API contract from OIS -------------------

    _PENDING = (
        "Not implemented yet. OIS still needs to confirm: the GraphQL "
        "mutation or endpoint for {what}, the request body it expects, and a "
        "sample response. Once they provide those this method is the only "
        "place that changes."
    )

    def upload_sbom(self, document: Dict[str, Any], *, name: str) -> str:
        """Submit one CycloneDX document. Returns the scan identifier."""
        raise WizNotConfiguredError(self._PENDING.format(what="uploading an SBOM"))

    def fetch_findings(self, scan_id: str) -> List[Dict[str, Any]]:
        """Return findings for a completed scan."""
        raise WizNotConfiguredError(self._PENDING.format(what="reading findings"))
