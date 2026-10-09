"""Shared fixtures. Everything here is SYNTHETIC: invented client IDs, tokens and health data."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeHTTP, FakeTokenServer  # noqa: E402

from fitbit_mcp import client as client_module  # noqa: E402
from fitbit_mcp.config import load_settings  # noqa: E402
from fitbit_mcp.oauth import TokenStore  # noqa: E402
from fitbit_mcp.server import build_app, create_server  # noqa: E402

TODAY = "2026-10-07"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("FITBIT_MCP_TODAY", TODAY)
    for name in ("FITBIT_CLIENT_ID", "FITBIT_CLIENT_SECRET", "GOOGLE_CLIENT_SECRETS", "FITBIT_MCP_CONFIG",
                 "CLAUDE_PROJECT_DIR", "FITBIT_MCP_GHEALTH_CREDENTIALS",
                 "FITBIT_MCP_HISTORY_FLOOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path / "home"))
    # Never touch a real ~/.config/ghealth during tests; use a synthetic, initially empty one.
    monkeypatch.setenv("FITBIT_MCP_GHEALTH_DIR", str(tmp_path / "ghealth"))
    monkeypatch.setenv("FITBIT_MCP_TZ", "UTC")
    monkeypatch.setenv("FITBIT_MCP_MIN_REQUEST_INTERVAL", "0")
    monkeypatch.setenv("FITBIT_MCP_JOB_HEARTBEAT_SECONDS", "0.2")
    monkeypatch.setenv("FITBIT_MCP_JOB_STALE_SECONDS", "1")
    monkeypatch.setenv("FITBIT_MCP_AUTO_RESUME", "0")
    client_module._NOT_BEFORE.clear()
    yield
    client_module._NOT_BEFORE.clear()


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    (home / "config.json").write_text(json.dumps({"fitbit_client_id": "SYNTHCLIENT",
                                                  "fitbit_redirect_uri": "http://127.0.0.1:{}/callback".format(port)}))
    (home / "client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "synthetic.apps.googleusercontent.com", "client_secret": "synthetic-desktop-secret"}}))
    return load_settings(home)


@pytest.fixture
def http():
    return FakeHTTP()


def seed_token(settings, provider, scope, refresh="synthetic-refresh-0", expires_in=28800, server=None):
    if server is not None:
        server.valid_refresh.add(refresh)
    TokenStore(settings, provider).save({
        "provider": provider, "access_token": "synthetic-access-0", "refresh_token": refresh,
        "expires_at": time.time() + expires_in, "scope": scope, "user_id": "SYNTH01", "status": "active",
        "obtained_at": "2026-10-01T00:00:00Z"})


@pytest.fixture
def make_app(settings, http):
    created = []

    def factory(open_browser=None):
        app = build_app(settings, http=http, open_browser=open_browser)
        created.append(app)
        return app
    yield factory


@pytest.fixture
def fitbit_tokens(settings, http):
    server = FakeTokenServer(http, "fitbit", scope="activity heartrate sleep weight profile")
    seed_token(settings, "fitbit", server.scope, server=server)
    return server


@pytest.fixture
def google_tokens(settings, http):
    from fitbit_mcp import catalog
    scope = " ".join(catalog.google_scope(s) for s in ("activity_and_fitness", "sleep",
                                                       "health_metrics_and_measurements", "profile", "settings"))
    server = FakeTokenServer(http, "google", scope=scope)
    seed_token(settings, "google", scope, server=server)
    return server


__all__ = ["seed_token", "create_server", "TODAY"]
