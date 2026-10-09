"""OAuth 2.0 (authorization code + PKCE + state) with a 127.0.0.1 loopback callback.

Ported from the exporter's FitbitAuth / pkce / callback_values and improved:
* one implementation for Fitbit and Google, so the Google flow no longer depends on
  google-auth-oauthlib (whose run_local_server prints to stdout and cannot be cancelled);
* cancellable, non-blocking (runs inside a job thread);
* refresh-token rotation is serialized with a thread lock *and* an OS file lock and
  re-reads the token file inside the lock, so concurrent jobs or a second server process
  never spend the same single-use Fitbit refresh token twice;
* tokens never leave this module except as an Authorization header value.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from . import catalog
from .config import Settings
from .util import log, read_json, save, utcnow


class AuthError(Exception):
    """Authentication problem with a user-safe message (never contains secrets)."""


class AuthRequired(AuthError):
    """No usable credentials; the user must run connect_account."""


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ primitives (ported)

def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def callback_values(path: str, expected_path: str, state: str) -> dict[str, Any]:
    parsed = urlsplit(path)
    query = parse_qs(parsed.query)
    if parsed.path != expected_path:
        raise AuthError("Wrong callback path")
    if not secrets.compare_digest(query.get("state", [""])[0], state):
        raise AuthError("Invalid OAuth state")
    if "error" in query:
        return {"denied": True, "error": query["error"][0][:64]}
    code = query.get("code", [""])[0]
    if not code:
        raise AuthError("Missing authorization code")
    result: dict[str, Any] = {"code": code}
    if "scope" in query:
        result["scope"] = query["scope"][0]
    return result


def provider_error(response: Any) -> str:
    """Provider error codes only; never echo credentials or response bodies."""
    try:
        payload = response.json()
        if isinstance(payload.get("error"), dict):
            err = payload["error"]
            reasons = [d.get("reason") for d in err.get("details", []) if isinstance(d, dict) and d.get("reason")]
            return ", ".join([str(err.get("status") or "API request failed")] + [str(r)[:60] for r in reasons])
        if isinstance(payload.get("error"), str):
            return payload["error"][:100]
        errors = payload.get("errors", [])
        return ", ".join(str(e.get("errorType", "error"))[:60] for e in errors if isinstance(e, dict)) \
            or "API request failed"
    except (ValueError, AttributeError, TypeError):
        return "API request failed"


# --------------------------------------------------------------------- token storage

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class TokenStore:
    def __init__(self, settings: Settings, provider: str):
        self.provider = provider
        self.path: Path = settings.credentials_dir / (provider + ".json")
        self.lock_path: Path = settings.credentials_dir / (provider + ".lock")
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.path), threading.RLock())

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = read_json(self.path)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, token: dict[str, Any]) -> None:
        save(self.path, token)

    def delete(self) -> bool:
        existed = self.path.exists()
        if existed:
            self.path.unlink()
        return existed

    @contextmanager
    def locked(self, timeout: float = 60.0):
        """Thread lock + OS advisory lock, released automatically if the process dies."""
        if not self._thread_lock.acquire(timeout=timeout):
            raise AuthError("Timed out waiting for the credential lock.")
        try:
            self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(self.lock_path, "a+b") as handle:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        _os_lock(handle)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise AuthError("Another process is refreshing these credentials; try again.")
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    _os_unlock(handle)
        finally:
            self._thread_lock.release()


def _os_lock(handle) -> None:
    if os.name == "nt":  # pragma: no cover - Windows
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(handle) -> None:
    if os.name == "nt":  # pragma: no cover - Windows
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


# ------------------------------------------------------------------- provider details

def provider_client(settings: Settings, provider: str) -> dict[str, Any]:
    if provider == "fitbit":
        client_id = settings.fitbit_client_id()
        if not client_id:
            raise AuthError("Fitbit client ID is not configured. Set fitbit_client_id in {} "
                            "(see README).".format(settings.config_path))
        return {"client_id": client_id, "client_secret": settings.fitbit_client_secret(),
                "authorize": catalog.FITBIT_AUTHORIZE, "token": catalog.FITBIT_TOKEN,
                "revoke": catalog.FITBIT_REVOKE}
    if provider == "google":
        client = settings.google_client()
        if not client:
            raise AuthError("Google OAuth Desktop-app credentials not found at {}. Download them from "
                            "your approved Google Cloud project (see README).".format(
                                settings.google_client_secrets_path()))
        token_uri = client.get("token_uri") or catalog.GOOGLE_TOKEN
        if urlsplit(token_uri).hostname != "oauth2.googleapis.com":
            token_uri = catalog.GOOGLE_TOKEN  # never send a refresh token to a host named by a file
        return {"client_id": client["client_id"], "client_secret": client["client_secret"],
                "authorize": catalog.GOOGLE_AUTHORIZE, "token": token_uri,
                "revoke": catalog.GOOGLE_REVOKE, "type": client.get("type", "installed")}
    raise AuthError("Unknown provider: " + str(provider))


def check_fitbit_available(settings: Settings) -> None:
    if settings.today() >= settings.fitbit_shutdown():
        raise AuthError("The legacy Fitbit Web API was turned off on {}. Use an approved Google Health "
                        "API project or import_takeout.".format(settings.fitbit_shutdown().isoformat()))


def _token_request(client: dict[str, Any], provider: str, fields: dict[str, str], http=requests) -> dict:
    fields = dict(fields, client_id=client["client_id"])
    auth = None
    if provider == "fitbit" and client.get("client_secret"):
        auth = (client["client_id"], client["client_secret"])
    elif provider == "google" and client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    try:
        response = http.post(client["token"], data=fields, auth=auth, timeout=60, allow_redirects=False)
    except requests.RequestException:
        raise AuthError("Could not reach the provider's token endpoint (network error).") from None
    if response.status_code != 200:
        reason = provider_error(response)
        if response.status_code in (400, 401) and ("invalid_grant" in reason or "invalid_token" in reason
                                                   or response.status_code == 401):
            raise AuthRequired("The provider rejected the stored authorization ({}). Run connect_account "
                               "again.".format(reason))
        raise AuthError("Token request failed (HTTP {}: {}).".format(response.status_code, reason))
    try:
        payload = response.json()
    except ValueError:
        raise AuthError("Token endpoint returned invalid JSON.") from None
    if not payload.get("access_token"):
        raise AuthError("Token response was incomplete.")
    return payload


def _merge_token(provider: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    if provider == "google" and new.get("scope"):
        new = dict(new, scope=" ".join(_normalize_scopes(new["scope"])))
    token = {
        "provider": provider,
        "access_token": new["access_token"],
        # Fitbit rotates refresh tokens on every refresh; Google usually returns none on refresh.
        "refresh_token": new.get("refresh_token") or old.get("refresh_token"),
        "expires_at": time.time() + float(new.get("expires_in", 3600)),
        "scope": new.get("scope", old.get("scope", "")),
        "requested_scopes": old.get("requested_scopes", []),
        "user_id": new.get("user_id", old.get("user_id")),
        "obtained_at": old.get("obtained_at", utcnow()),
        "refreshed_at": utcnow(),
        "status": "active",
    }
    if old.get("seeded_from"):
        token["seeded_from"] = old["seeded_from"]
    return token



# ------------------------------------------------- ghealth CLI credentials (Google)
# Ported from health-coach-app/auth.py: keep our *own* token file, seed it once from the
# `ghealth` CLI's credentials (same OAuth client), never write to the CLI's file, and if a
# refresh fails, retry once with the CLI's refresh token (it may be newer after
# `ghealth auth login`), so a Testing-mode 7-day expiry self-heals.

GHEALTH_REAUTH = ("Re-authorise with the CLI (`ghealth auth login`), then call connect_account again. While the "
                  "OAuth consent screen is in Testing mode Google expires refresh tokens after 7 days.")


def _normalize_scopes(scopes: Any) -> list[str]:
    if isinstance(scopes, str):
        scopes = scopes.split()
    out = []
    for scope in scopes or []:
        out.append(scope if str(scope).startswith("https://") else catalog.GOOGLE_SCOPE_PREFIX + str(scope))
    return out


def _expiry_ts(raw: Any) -> float:
    from datetime import datetime, timezone as tz
    if not isinstance(raw, str) or not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:  # google-auth writes naive UTC
        parsed = parsed.replace(tzinfo=tz.utc)
    return parsed.timestamp()


def read_ghealth_credentials(settings: Settings) -> dict[str, Any] | None:
    """Read-only view of the CLI's credentials, converted to our token shape."""
    path = settings.ghealth_credentials_path()
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None
    return {"provider": "google", "access_token": data.get("access_token") or data.get("token"),
            "refresh_token": data["refresh_token"], "expires_at": _expiry_ts(data.get("expiry")),
            "scope": " ".join(_normalize_scopes(data.get("scopes") or data.get("scope"))),
            "requested_scopes": [], "user_id": None, "obtained_at": utcnow(), "status": "active",
            "seeded_from": "ghealth CLI credentials"}


def seed_google_from_ghealth(settings: Settings) -> bool:
    """Copy the CLI's credentials into our own token file (only if we have none)."""
    store = TokenStore(settings, "google")
    with store.locked():
        current = store.load()
        if current.get("refresh_token") and current.get("status") != "invalid":
            return False
        seeded = read_ghealth_credentials(settings)
        if not seeded:
            return False
        store.save(seeded)
        log.info("Seeded Google token from the ghealth CLI credentials")
        return True

class OAuthSession:
    """Supplies bearer tokens to the API client; handles refresh and rotation safely."""

    def __init__(self, settings: Settings, provider: str, http=requests):
        self.settings, self.provider, self.http = settings, provider, http
        self.store = TokenStore(settings, provider)
        self.base = catalog.FITBIT_API if provider == "fitbit" else catalog.GOOGLE_API

    def token(self) -> dict[str, Any]:
        return self.store.load()

    def granted_scopes(self) -> set[str]:
        return set(self.store.load().get("scope", "").split())

    def bearer(self) -> str:
        token = self.store.load()
        if self.provider == "google" and not token.get("refresh_token"):
            seed_google_from_ghealth(self.settings)
            token = self.store.load()
        if token.get("access_token") and token.get("expires_at", 0) > time.time() + 60 \
                and token.get("status", "active") == "active":
            return token["access_token"]
        with self.store.locked():
            token = self.store.load()  # another job/process may have refreshed while we waited
            if token.get("access_token") and token.get("expires_at", 0) > time.time() + 60 \
                    and token.get("status", "active") == "active":
                return token["access_token"]
            return self._refresh_locked(token)["access_token"]

    def force_refresh(self, stale_access_token: str) -> str:
        """After a 401: refresh unless someone already replaced the stale token."""
        with self.store.locked():
            token = self.store.load()
            if token.get("access_token") and token["access_token"] != stale_access_token \
                    and token.get("expires_at", 0) > time.time() + 60:
                return token["access_token"]
            return self._refresh_locked(token)["access_token"]

    def _refresh_locked(self, token: dict[str, Any]) -> dict[str, Any]:
        if self.provider == "google" and (not token.get("refresh_token") or token.get("status") == "invalid"):
            cli = read_ghealth_credentials(self.settings)
            if cli and cli["refresh_token"] != token.get("refresh_token"):
                token = cli   # e.g. the user just re-ran `ghealth auth login`
        if not token.get("refresh_token") or token.get("status") == "invalid":
            raise AuthRequired("Not connected to {}. Run connect_account first.".format(self.provider))
        if self.provider == "fitbit":
            check_fitbit_available(self.settings)
        client = provider_client(self.settings, self.provider)
        try:
            new = _token_request(client, self.provider,
                                 {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]},
                                 http=self.http)
        except AuthRequired:
            cli = read_ghealth_credentials(self.settings) if self.provider == "google" else None
            if cli and cli["refresh_token"] != token.get("refresh_token"):
                log.warning("Google refresh failed; retrying with the ghealth CLI's newer credentials")
                try:
                    new = _token_request(client, self.provider, {"grant_type": "refresh_token",
                                                                 "refresh_token": cli["refresh_token"]},
                                         http=self.http)
                    merged = _merge_token(self.provider, cli, new)
                    self.store.save(merged)
                    return merged
                except AuthRequired:
                    pass
            token["status"] = "invalid"
            self.store.save(token)
            if self.provider == "google":
                raise AuthRequired("Google rejected the stored authorization. " + GHEALTH_REAUTH) from None
            raise
        merged = _merge_token(self.provider, token, new)
        self.store.save(merged)  # persist the rotated refresh token before any further call
        log.info("Refreshed %s access token", self.provider)
        return merged


# ------------------------------------------------------------------------ login flow

def _validate_fitbit_redirect(redirect: str):
    parsed = urlsplit(redirect)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.query
            or parsed.fragment or parsed.username):
        raise AuthError("fitbit_redirect_uri must be http://127.0.0.1:PORT/callback for this local tool.")
    return parsed


def run_login(settings: Settings, provider: str, scopes: list[str], *, cancel: threading.Event,
              report: Callable[[dict[str, Any]], None], open_browser: Callable[[str], bool] = webbrowser.open,
              http=requests, timeout: float = 300.0) -> dict[str, Any]:
    """Interactive browser sign-in. Returns a summary with granted scopes; stores tokens."""
    if provider == "fitbit":
        check_fitbit_available(settings)
    client = provider_client(settings, provider)
    if provider == "google" and client.get("type") != "installed":
        raise AuthError("Browser sign-in needs a Desktop-app OAuth client ('installed'); use the ghealth CLI's "
                        "credentials instead or download a Desktop-app client.")
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(32)
    result: dict[str, Any] = {}

    if provider == "fitbit":
        parsed = _validate_fitbit_redirect(settings.fitbit_redirect_uri())
        host_port, expected_path = parsed.port, parsed.path or "/"
    else:
        host_port, expected_path = 0, "/"  # Google desktop clients accept any loopback port

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # callback URLs contain one-time codes: never log them
            pass

        def do_GET(self):
            try:
                values = callback_values(self.path, expected_path, state)
            except AuthError:
                self.send_error(400, "Invalid callback")
                return
            result.update(values)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Sign-in received. Return to Claude Code; you can close this tab.")

    try:
        server = HTTPServer(("127.0.0.1", host_port), Handler)
    except OSError:
        raise AuthError("Port {} on 127.0.0.1 is in use; close the other program or change the redirect "
                        "URI.".format(host_port)) from None
    with server:
        server.timeout = 0.5
        port = server.server_address[1]
        redirect = settings.fitbit_redirect_uri() if provider == "fitbit" else "http://127.0.0.1:{}/".format(port)
        params = dict(client_id=client["client_id"], response_type="code", redirect_uri=redirect,
                      scope=" ".join(scopes), state=state, code_challenge=challenge,
                      code_challenge_method="S256")
        if provider == "google":
            params.update(access_type="offline", prompt="consent", include_granted_scopes="true")
        url = client["authorize"] + "?" + urlencode(params)
        try:
            opened = bool(open_browser(url))
        except Exception:  # pragma: no cover - platform specific
            opened = False
        report({"stage": "waiting_for_browser_consent", "browser_opened": opened,
                "authorization_url": url, "expires_in_seconds": int(timeout)})
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            if cancel.is_set():
                raise Cancelled()
            server.handle_request()
    if not result:
        raise AuthError("Sign-in timed out after {} seconds. Run connect_account again.".format(int(timeout)))
    if result.get("denied"):
        raise AuthError("Sign-in was declined at the consent screen ({}); no access was granted."
                        .format(result.get("error", "access_denied")))
    report({"stage": "exchanging_code"})
    new = _token_request(client, provider, dict(grant_type="authorization_code", code=result["code"],
                                                code_verifier=verifier, redirect_uri=redirect), http=http)
    if not new.get("refresh_token"):
        raise AuthError("The provider did not return a refresh token; long imports could not continue. "
                        "Revoke the app's access in your account settings and connect again.")
    store = TokenStore(settings, provider)
    with store.locked():
        token = _merge_token(provider, {"requested_scopes": scopes, "obtained_at": utcnow()}, new)
        if not new.get("scope") and result.get("scope"):
            token["scope"] = result["scope"]
        store.save(token)
    granted = set(token["scope"].split())
    return {"granted_scopes": sorted(granted), "denied_scopes": sorted(set(scopes) - granted)}


def revoke(settings: Settings, provider: str, http=requests) -> dict[str, Any]:
    """Revoke the stored authorization at the provider. Never sends health data."""
    store = TokenStore(settings, provider)
    with store.locked():
        token = store.load()
        secret_token = token.get("refresh_token") or token.get("access_token")
        if not secret_token:
            return {"revoked": False, "reason": "no stored token"}
        client = provider_client(settings, provider)
        data = {"token": secret_token}
        auth = None
        if provider == "fitbit":
            if client.get("client_secret"):
                auth = (client["client_id"], client["client_secret"])
            else:
                data["client_id"] = client["client_id"]
        try:
            response = http.post(client["revoke"], data=data, auth=auth, timeout=30, allow_redirects=False)
        except requests.RequestException:
            return {"revoked": False, "reason": "network error contacting the provider"}
        if response.status_code == 200:
            return {"revoked": True}
        if provider == "fitbit" and response.status_code == 404:
            return {"revoked": True, "reason": "token was already unknown to Fitbit"}
        return {"revoked": False, "reason": "HTTP {}: {}".format(response.status_code, provider_error(response))}
