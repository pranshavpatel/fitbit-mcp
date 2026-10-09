"""Authentication safeguards (SYNTHETIC credentials and loopback callbacks only)."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import seed_token
from fakes import FakeResponse, FakeTokenServer

from fitbit_mcp import oauth
from fitbit_mcp.oauth import AuthError, AuthRequired, OAuthSession, TokenStore


def test_pkce_verifier_and_challenge():
    verifier, challenge = oauth.pkce()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert oauth.pkce()[0] != verifier


def test_callback_rejects_wrong_state_path_and_missing_code():
    for url in ["/callback?code=abc&state=bad", "/wrong?code=abc&state=good", "/callback?state=good"]:
        with pytest.raises(AuthError):
            oauth.callback_values(url, "/callback", "good")
    assert oauth.callback_values("/callback?code=abc&state=good", "/callback", "good")["code"] == "abc"
    assert oauth.callback_values("/callback?error=access_denied&state=good", "/callback", "good")["denied"]


def _browser(responses, code="synthetic-code", tamper_first=False, deny=False):
    """SYNTHETIC browser: follows the authorization URL straight to the loopback callback."""
    def open_browser(url):
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["code_challenge_method"] == "S256" and query["code_challenge"]
        responses.append(query)

        def hit():
            time.sleep(0.2)
            base = query["redirect_uri"]
            if tamper_first:
                try:
                    urllib.request.urlopen(base + "?code=attacker&state=forged", timeout=5)
                except urllib.error.HTTPError as error:
                    responses.append(("forged", error.code))
            suffix = "error=access_denied" if deny else "code=" + code
            sep = "&" if "?" in base else "?"
            urllib.request.urlopen(base + sep + suffix + "&state=" + query["state"], timeout=5).read()
        threading.Thread(target=hit, daemon=True).start()
        return True
    return open_browser


def test_fitbit_login_validates_state_and_stores_token_privately(settings, http):
    FakeTokenServer(http, "fitbit", scope="activity sleep")
    seen = []
    reports = []
    summary = oauth.run_login(settings, "fitbit", ["activity", "sleep", "heartrate"], cancel=threading.Event(),
                              report=reports.append, open_browser=_browser(seen, tamper_first=True), http=http,
                              timeout=10)
    assert ("forged", 400) in seen                       # forged state rejected, flow continued
    assert summary["granted_scopes"] == ["activity", "sleep"]
    assert summary["denied_scopes"] == ["heartrate"]     # partial consent recorded
    token_path = TokenStore(settings, "fitbit").path
    assert oct(token_path.stat().st_mode & 0o777) == "0o600"
    post = [c for c in http.calls if c[0] == "POST"][0]
    assert post[2]["code_verifier"] and post[2]["grant_type"] == "authorization_code"
    assert all("synthetic-access" not in json.dumps(r) for r in reports)   # progress never carries tokens


def test_google_login_uses_random_loopback_port_and_offline_access(settings, http):
    FakeTokenServer(http, "google", scope="https://www.googleapis.com/auth/googlehealth.sleep.readonly")
    seen = []
    oauth.run_login(settings, "google", ["https://www.googleapis.com/auth/googlehealth.sleep.readonly"],
                    cancel=threading.Event(), report=lambda _: None, open_browser=_browser(seen), http=http,
                    timeout=10)
    query = seen[0]
    assert query["redirect_uri"].startswith("http://127.0.0.1:") and query["access_type"] == "offline"
    assert all(s.endswith(".readonly") for s in query["scope"].split())


def test_declined_consent_reports_failure(settings, http):
    FakeTokenServer(http, "fitbit")
    with pytest.raises(AuthError, match="declined"):
        oauth.run_login(settings, "fitbit", ["activity"], cancel=threading.Event(), report=lambda _: None,
                        open_browser=_browser([], deny=True), http=http, timeout=10)
    assert not TokenStore(settings, "fitbit").path.exists()


def test_login_can_be_cancelled(settings, http):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(oauth.Cancelled):
        oauth.run_login(settings, "fitbit", ["activity"], cancel=cancel, report=lambda _: None,
                        open_browser=lambda url: True, http=http, timeout=30)


def test_refresh_rotation_is_persisted_and_serialized(settings, http):
    server = FakeTokenServer(http, "fitbit")
    seed_token(settings, "fitbit", "activity", expires_in=-10, server=server)
    sessions = [OAuthSession(settings, "fitbit", http=http) for _ in range(8)]
    results, errors = [], []

    def worker(session):
        try:
            results.append(session.bearer())
        except Exception as error:  # pragma: no cover - would fail the test below
            errors.append(error)
    threads = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    refreshes = [c for c in http.calls if c[0] == "POST" and c[2].get("grant_type") == "refresh_token"]
    assert len(refreshes) == 1            # single-use refresh token spent exactly once
    assert len(set(results)) == 1
    stored = TokenStore(settings, "fitbit").load()
    assert stored["refresh_token"] == "synthetic-refresh-1" and stored["user_id"] == "SYNTH01"


def test_force_refresh_after_401_skips_if_another_job_already_rotated(settings, http):
    server = FakeTokenServer(http, "fitbit")
    seed_token(settings, "fitbit", "activity", server=server)
    session = OAuthSession(settings, "fitbit", http=http)
    first = session.force_refresh("synthetic-access-0")
    second = session.force_refresh("synthetic-access-0")   # stale token from a concurrent job
    assert first == second
    assert len([c for c in http.calls if c[0] == "POST"]) == 1


def test_revoked_refresh_token_requires_reconnect(settings, http):
    FakeTokenServer(http, "fitbit")
    seed_token(settings, "fitbit", "activity", refresh="revoked-token", expires_in=-10)
    with pytest.raises(AuthRequired):
        OAuthSession(settings, "fitbit", http=http).bearer()
    assert TokenStore(settings, "fitbit").load()["status"] == "invalid"


def test_google_refresh_keeps_refresh_token_when_not_rotated(settings, http):
    server = FakeTokenServer(http, "google", scope="x")
    seed_token(settings, "google", "x", expires_in=-10, server=server)
    OAuthSession(settings, "google", http=http).bearer()
    stored = TokenStore(settings, "google").load()
    assert stored["refresh_token"] == "synthetic-refresh-0"
    post = [c for c in http.calls if c[0] == "POST"][0]
    assert post[2]["client_secret"] == "synthetic-desktop-secret"


def test_fitbit_api_refused_after_shutdown(settings, http, monkeypatch):
    monkeypatch.setenv("FITBIT_MCP_TODAY", "2026-10-30")
    with pytest.raises(AuthError, match="turned off"):
        oauth.run_login(settings, "fitbit", ["activity"], cancel=threading.Event(), report=lambda _: None,
                        open_browser=lambda url: True, http=http, timeout=1)


def test_redact_removes_tokens_from_messages():
    text = oauth.provider_error(FakeResponse(400, {"error": "invalid_grant"}))
    assert text == "invalid_grant"
    from fitbit_mcp.util import redact
    message = redact("Authorization: Bearer abc.def refresh_token=xyz&code=123 client_secret: s3cr3t")
    for secret in ("abc.def", "xyz", "123", "s3cr3t"):
        assert secret not in message


def test_revoke_uses_public_client_form_for_pkce_apps(settings, http):
    seed_token(settings, "fitbit", "activity")
    http.on_post("oauth2/revoke", lambda url, data, auth: FakeResponse(200, {}))
    assert oauth.revoke(settings, "fitbit", http=http)["revoked"] is True
    call = [c for c in http.calls if "revoke" in c[1]][0]
    assert call[2]["client_id"] == "SYNTHCLIENT" and call[3] is None


# ------------------------------------------- ghealth CLI credentials (health-coach-app's approach)

def write_cli_credentials(settings, refresh, scopes=("sleep.readonly", "activity_and_fitness.readonly")):
    """SYNTHETIC ghealth CLI credentials file (same shape health-coach-app reads)."""
    path = settings.ghealth_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": "cli-access", "refresh_token": refresh, "token_type": "Bearer",
                                "expiry": "2026-01-01T00:00:00", "scopes": list(scopes)}))
    return path


def test_google_seeds_own_token_from_ghealth_cli_without_touching_it(settings, http):
    server = FakeTokenServer(http, "google", scope="https://www.googleapis.com/auth/googlehealth.sleep.readonly")
    server.valid_refresh.add("cli-refresh-1")
    cli = write_cli_credentials(settings, "cli-refresh-1")
    before = cli.read_bytes()
    token = OAuthSession(settings, "google", http=http).bearer()
    assert token == "synthetic-access-1"                       # expired CLI access token was refreshed
    stored = TokenStore(settings, "google").load()
    assert stored["refresh_token"] == "cli-refresh-1" and stored["seeded_from"] == "ghealth CLI credentials"
    assert "https://www.googleapis.com/auth/googlehealth.sleep.readonly" in stored["scope"].split()
    assert cli.read_bytes() == before                           # the CLI's file is never written


def test_google_refresh_failure_self_heals_from_newer_cli_login(settings, http):
    server = FakeTokenServer(http, "google", scope="x")
    seed_token(settings, "google", "x", refresh="app-refresh-expired", expires_in=-10)   # not valid at Google
    server.valid_refresh.add("cli-refresh-new")
    write_cli_credentials(settings, "cli-refresh-new")          # user re-ran `ghealth auth login`
    assert OAuthSession(settings, "google", http=http).bearer() == "synthetic-access-1"
    assert TokenStore(settings, "google").load()["refresh_token"] == "cli-refresh-new"


def test_google_expired_everywhere_gives_actionable_message(settings, http):
    FakeTokenServer(http, "google", scope="x")
    seed_token(settings, "google", "x", refresh="dead", expires_in=-10)
    write_cli_credentials(settings, "also-dead")
    with pytest.raises(AuthRequired, match="ghealth auth login"):
        OAuthSession(settings, "google", http=http).bearer()


def test_client_secret_defaults_to_ghealth_dir(settings):
    (settings.home / "client_secret.json").unlink()
    path = settings.ghealth_dir() / "client_secret.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installed": {"client_id": "cli.apps.googleusercontent.com", "client_secret": "s"}}))
    assert settings.google_client_secrets_path() == path
    assert settings.google_client()["client_id"] == "cli.apps.googleusercontent.com"


def test_rollup_is_the_only_post_the_api_client_allows(settings, http):
    from fitbit_mcp.client import ApiClient, ApiError
    seed_token(settings, "google", "x")
    client = ApiClient(OAuthSession(settings, "google", http=http), waiter=lambda s, r: None, http=http)
    with pytest.raises(ApiError):
        client.rollup("/v4/users/me/dataTypes/steps/dataPoints:batchDelete", {})
    with pytest.raises(ApiError):
        client.rollup("/v4/users/me/dataTypes/steps/dataPoints", {})


def test_placeholder_client_secret_setting_falls_back_to_ghealth(settings):
    settings.values["google_client_secrets"] = "missing_client_secret.json"   # old example config value
    path = settings.ghealth_dir() / "client_secret.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installed": {"client_id": "cli.apps.googleusercontent.com", "client_secret": "s"}}))
    assert settings.google_client_secrets_path() == path
