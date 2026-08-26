"""Tests for the Posit Connect API client, over real HTTP against a fake server.

The retry tests matter most: retrying a permanent auth failure would multiply
failed-authentication audit entries on the Connect server, so the boundary
between transient and permanent must be exact.
"""

from __future__ import annotations

import pytest

from clients.connect_client import ConnectClient
from exceptions import (
    ConnectAuthError,
    ConnectNotFoundError,
    ConnectRateLimitError,
    ConnectServerError,
)


@pytest.fixture
def client(settings):
    with ConnectClient(settings) as connect_client:
        yield connect_client


class TestConnectivity:
    def test_verify_connection_reports_version_and_identity(self, client):
        info = client.verify_connection()
        assert info["version"] == "2025.04.0"
        assert info["username"] == "srevanth"
        assert info["user_role"] == "administrator"

    def test_bad_api_key_raises_auth_error(self, settings):
        settings.connect_api_key = "wrong-key"  # type: ignore[assignment]
        with ConnectClient(settings) as bad_client:
            with pytest.raises(ConnectAuthError):
                bad_client.get_server_settings()


class TestContentDiscovery:
    def test_lists_all_content(self, client):
        items = client.list_content()
        assert len(items) == 3
        assert {item["guid"] for item in items} == {"c-treasury", "c-risk", "c-static"}

    def test_falls_back_when_include_unsupported(self, client, connect_state):
        """An older server rejects ?include=owner,tags with a 400."""
        connect_state.support_include = False
        items = client.list_content()
        assert len(items) == 3
        # Two calls: the include attempt, then the plain retry.
        assert connect_state.request_count["/__api__/v1/content"] == 2

    def test_uses_include_when_supported(self, client, connect_state):
        connect_state.support_include = True
        items = client.list_content()
        assert len(items) == 3
        assert connect_state.request_count["/__api__/v1/content"] == 1

    def test_get_single_content(self, client):
        item = client.get_content("c-treasury")
        assert item["title"] == "Treasury Dashboard"


class TestPackages:
    def test_fetches_package_manifest(self, client):
        packages = client.get_content_packages("c-treasury")
        assert len(packages) == 4  # includes the duplicate pandas entry
        assert {p["name"] for p in packages} == {"pandas", "numpy", "pillow"}

    def test_empty_manifest_is_not_an_error(self, client):
        assert client.get_content_packages("c-static") == []

    def test_missing_content_returns_empty_not_error(self, client):
        """A 404 on packages means 'no manifest', not a failed scan."""
        assert client.get_content_packages("does-not-exist") == []


class TestUsers:
    def test_bulk_user_listing_unwraps_envelope(self, client):
        users = client.list_users()
        assert {u["guid"] for u in users} == {"u-1", "u-2"}

    def test_get_user_is_cached(self, client, connect_state):
        client.get_user("u-1")
        client.get_user("u-1")
        client.get_user("u-1")
        assert connect_state.request_count["/__api__/v1/users/u-1"] == 1

    def test_prime_cache_avoids_lookups(self, client, connect_state):
        client.prime_user_cache([{"guid": "u-2", "username": "jdoe"}])
        assert client.get_user("u-2")["username"] == "jdoe"
        assert "/__api__/v1/users/u-2" not in connect_state.request_count


class TestRetryBehaviour:
    def test_retries_transient_server_error_then_succeeds(self, client, connect_state):
        connect_state.fail_status = 500
        connect_state.fail_times["/server_settings"] = 2  # max_retries=2 -> 3 attempts
        result = client.get_server_settings()
        assert result["version"] == "2025.04.0"
        assert connect_state.request_count["/__api__/server_settings"] == 3

    def test_gives_up_after_max_retries(self, client, connect_state):
        connect_state.fail_status = 500
        connect_state.fail_times["/server_settings"] = 99
        with pytest.raises(ConnectServerError):
            client.get_server_settings()
        assert connect_state.request_count["/__api__/server_settings"] == 3

    def test_retries_rate_limit(self, client, connect_state):
        connect_state.fail_status = 429
        connect_state.fail_times["/server_settings"] = 1
        client.get_server_settings()
        assert connect_state.request_count["/__api__/server_settings"] == 2

    def test_does_not_retry_auth_failure(self, settings, connect_state):
        """A 401 is permanent - retrying only spams the audit log."""
        settings.connect_api_key = "wrong-key"  # type: ignore[assignment]
        with ConnectClient(settings) as bad_client:
            with pytest.raises(ConnectAuthError):
                bad_client.get_server_settings()
        assert connect_state.request_count["/__api__/server_settings"] == 1

    def test_does_not_retry_not_found(self, client, connect_state):
        with pytest.raises(ConnectNotFoundError):
            client.get_content("nope")
        assert connect_state.request_count["/__api__/v1/content/nope"] == 1


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,expected",
        [(429, ConnectRateLimitError), (500, ConnectServerError),
         (503, ConnectServerError)],
    )
    def test_status_maps_to_exception(self, client, connect_state, status, expected):
        connect_state.fail_status = status
        connect_state.fail_times["/server_settings"] = 99
        with pytest.raises(expected):
            client.get_server_settings()
