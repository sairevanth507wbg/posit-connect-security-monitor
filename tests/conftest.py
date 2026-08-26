"""Shared pytest fixtures.

Provides a threaded in-process fake Posit Connect server so the client can be
exercised over real HTTP - real sockets, real status codes, real retries -
without needing a Connect instance.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

API_KEY = "test-api-key"

USERS: Dict[str, Dict[str, Any]] = {
    "u-1": {
        "guid": "u-1", "username": "srevanth", "first_name": "Sai",
        "last_name": "Revanth", "email": "sai@example.com",
    },
    "u-2": {
        "guid": "u-2", "username": "jdoe", "first_name": "Jane",
        "last_name": "Doe", "email": "jane@example.com",
    },
}

CONTENT: List[Dict[str, Any]] = [
    {
        "guid": "c-treasury", "name": "treasury-dashboard",
        "title": "Treasury Dashboard", "owner_guid": "u-1",
        "app_mode": "python-shiny", "bundle_id": 4271,
        "created_time": "2024-01-15T10:30:00Z",
        "last_deployed_time": "2025-06-02T14:05:11Z",
        "content_url": "https://connect.example.com/content/c-treasury/",
    },
    {
        "guid": "c-risk", "name": "risk-analytics", "title": "Risk Analytics",
        "owner_guid": "u-2", "app_mode": "quarto-static", "bundle_id": 5120,
        "created_time": "2023-11-01T08:00:00Z",
        "last_deployed_time": "2025-05-20T09:12:00Z",
    },
    {
        "guid": "c-static", "name": "static-docs", "title": "Static Docs",
        "owner_guid": "u-2", "app_mode": "static", "bundle_id": None,
        "created_time": "2022-03-09T11:00:00Z", "last_deployed_time": None,
    },
]

PACKAGES: Dict[str, List[Dict[str, Any]]] = {
    "c-treasury": [
        {"language": "python", "name": "pandas", "version": "2.2.2"},
        {"language": "python", "name": "numpy", "version": "2.1.0"},
        {"language": "python", "name": "pillow", "version": "10.2.0"},
        # Duplicate: Connect can list a package as both direct and transitive.
        {"language": "python", "name": "pandas", "version": "2.2.2"},
    ],
    "c-risk": [
        {"language": "r", "name": "dplyr", "version": "1.1.4"},
        {"language": "r", "name": "ggplot2", "version": "3.5.1"},
        {"language": "python", "name": "requests", "version": None},
    ],
    "c-static": [],
}


class FakeConnectState:
    """Mutable knobs the tests use to steer the fake server."""

    def __init__(self) -> None:
        self.request_count: Dict[str, int] = {}
        self.fail_times: Dict[str, int] = {}   # path fragment -> remaining failures
        self.fail_status: int = 500
        self.support_include: bool = False
        self.lock = threading.Lock()

    def record(self, path: str) -> int:
        with self.lock:
            self.request_count[path] = self.request_count.get(path, 0) + 1
            return self.request_count[path]

    def should_fail(self, path: str) -> bool:
        with self.lock:
            for fragment, remaining in self.fail_times.items():
                if fragment in path and remaining > 0:
                    self.fail_times[fragment] = remaining - 1
                    return True
        return False


def _make_handler(state: FakeConnectState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence test output
            pass

        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            # Record before authenticating so retry tests can count the
            # attempts made against a rejected key.
            state.record(path)

            if self.headers.get("Authorization") != "Key " + API_KEY:
                return self._send(401, {"error": "invalid API key"})

            if state.should_fail(path):
                return self._send(state.fail_status, {"error": "injected failure"})

            if path == "/__api__/server_settings":
                return self._send(200, {"version": "2025.04.0"})
            if path == "/__api__/v1/user":
                return self._send(200, {**USERS["u-1"], "user_role": "administrator"})
            if path == "/__api__/v1/content":
                if "include" in query and not state.support_include:
                    return self._send(400, {"error": "unsupported parameter: include"})
                return self._send(200, CONTENT)
            if path == "/__api__/v1/users":
                return self._send(
                    200, {"results": list(USERS.values()), "total": len(USERS)}
                )

            m = re.fullmatch(r"/__api__/v1/content/([^/]+)/packages", path)
            if m:
                guid = m.group(1)
                if guid not in PACKAGES:
                    return self._send(404, {"error": "not found"})
                return self._send(200, PACKAGES[guid])

            m = re.fullmatch(r"/__api__/v1/content/([^/]+)", path)
            if m:
                for item in CONTENT:
                    if item["guid"] == m.group(1):
                        return self._send(200, item)
                return self._send(404, {"error": "not found"})

            m = re.fullmatch(r"/__api__/v1/users/([^/]+)", path)
            if m:
                user = USERS.get(m.group(1))
                return self._send(200, user) if user else self._send(404, {"e": "no"})

            return self._send(404, {"error": "unknown endpoint " + path})

    return Handler


class _ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def connect_state() -> FakeConnectState:
    return FakeConnectState()


@pytest.fixture
def fake_connect(connect_state):
    """Run the fake Connect server; yields (base_url, state)."""
    server = _ThreadedServer(("127.0.0.1", 0), _make_handler(connect_state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield "http://" + host + ":" + str(port), connect_state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def settings(fake_connect, monkeypatch):
    """Settings pointed at the fake server, with retries fast enough for tests."""
    base_url, _ = fake_connect
    from config.settings import Settings

    return Settings(
        connect_server_url=base_url,
        connect_api_key=API_KEY,
        postgres_host="localhost",
        postgres_port=5432,
        postgres_db="test",
        postgres_user="test",
        postgres_password="test",
        max_retries=2,
        retry_initial_wait=0.01,
        retry_max_wait=0.05,
    )
