"""Local stdio MCP server for your own Fitbit / Google Health data.

stdout carries only MCP protocol messages: the SDK's stdio transport serves the wire from a
private descriptor and points fd 1 at stderr while serving, and all diagnostics use the
stderr logger. Health data and credentials stay in FITBIT_MCP_HOME on this computer.
"""
from __future__ import annotations

import hashlib
import os
import sys
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import catalog, queries, summaries
from .client import ApiClient, ApiError, NetworkError, RateLimitExhausted
from .config import ConfigError, Settings, load_settings
from .importer import Importer
from .jobs import JobManager
from .oauth import AuthError, OAuthSession, TokenStore, read_ghealth_credentials, revoke
from .store import Store
from .util import configure_logging, log, redact

INSTRUCTIONS = """Local, read-only access to the user's own Fitbit / Google Health data.
- Start with connection_status. connect_account, start_import, sync_data and import_takeout return a job_id
  immediately; poll job_status. Never tell the user an account is connected or data is imported unless
  job_status shows the job succeeded (or partial, with the missing categories explained).
- Remote access is read-only. disconnect_account needs a confirmation_token that you obtain in a first call
  and may only send after the user explicitly agrees in the conversation.
- Use list_data_types before querying. query_data and summarize_data read only the local database.
- Summaries explain their definition, coverage and exclusions; relay those caveats. Missing days are not zero.
- This is not medical advice; suggest a clinician for health concerns."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                              open_world_hint=True)

Provider = Literal["google", "fitbit"]
DateStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")]
Category = Literal[tuple(catalog.ALL_IMPORT_CATEGORIES)]  # type: ignore[valid-type]


@dataclass
class App:
    settings: Settings
    store: Store
    jobs: JobManager
    importer: Importer
    http: Any = None
    confirmations: dict = field(default_factory=dict)
    confirm_lock: Any = field(default_factory=threading.Lock)


def _short_scope(scope: str) -> str:
    if scope.startswith(catalog.GOOGLE_SCOPE_PREFIX):
        return scope[len(catalog.GOOGLE_SCOPE_PREFIX):].removesuffix(".readonly")
    return scope


def _job_view(job: dict | None) -> dict[str, Any]:
    if job is None:
        raise ToolError("Unknown job_id.")
    params = {k: v for k, v in (job.get("params") or {}).items() if not k.startswith("_") and k != "scopes"}
    view = {"job_id": job["id"], "kind": job["kind"], "provider": job["provider"], "status": job["status"],
            "message": job.get("message"), "parameters": params, "progress": job.get("progress") or {},
            "created_at": job["created_at"], "updated_at": job["updated_at"]}
    for key in ("error", "wait_until", "started_at", "finished_at"):
        if job.get(key):
            view[key] = job[key]
    if job.get("warnings"):
        view["warnings"] = job["warnings"][-20:]
        view["warning_count"] = len(job["warnings"])
    if job.get("stalled"):
        view["note"] = ("The server process running this job stopped. A running server takes it over within a "
                        "couple of minutes; resume_job does it immediately. Completed requests are kept.")
        view["next_step"] = "resume_job"
    elif job["status"] in ("queued", "running", "waiting_rate_limit"):
        view["next_step"] = "Poll job_status; cancel_job stops it safely."
    elif job["status"] in ("interrupted", "failed", "cancelled", "partial") and job["kind"] != "connect":
        view["next_step"] = "resume_job continues and retries failed requests; completed requests are kept."
    return view


def build_app(settings: Settings | None = None, http: Any = None, open_browser: Any = None) -> App:
    settings = settings or load_settings()
    store = Store(settings.db_path, settings.home)
    importer = Importer(settings, store, http=http, open_browser=open_browser)
    runners = {"connect": importer.run_connect, "import": importer.run_api, "sync": importer.run_api,
               "takeout": importer.run_takeout}
    jobs = JobManager(store, runners)
    interrupted = jobs.recover()   # only jobs whose owning server process has stopped heart-beating
    if interrupted:
        log.info("Recovered %d job(s) left by a stopped server process", len(interrupted))
    jobs.start()
    return App(settings=settings, store=store, jobs=jobs, importer=importer, http=http, confirmations={})


def _wrap(fn):
    """Translate internal errors into user-safe ToolErrors (no secrets, no stack traces)."""
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, KeyError, AuthError, ConfigError) as error:
            message = str(error) if not isinstance(error, KeyError) else "Not found: " + str(error)
            raise ToolError(redact(message)) from None
    return call


def create_server(app: App | None = None) -> MCPServer:
    app = app or build_app()
    mcp = MCPServer("fitbit-local", instructions=INSTRUCTIONS, version="0.1.0")

    # ---------------------------------------------------------------- connection
    @mcp.tool(annotations=READ_ONLY)
    def connection_status(provider: Literal["google", "fitbit", "all"] = "all",
                          check_remote: Annotated[bool, Field(description="Make one read-only API request to "
                                                                          "confirm the connection works")] = False
                          ) -> dict[str, Any]:
        """Report which provider is connected, granted permissions and connection health. Never returns tokens."""
        return _wrap(_connection_status)(app, provider, check_remote)

    @mcp.tool(annotations=LOCAL_WRITE)
    def connect_account(provider: Annotated[Provider | None, Field(description="Omit to pick the configured, "
                                                                              "currently available provider")] = None,
                        permissions: Annotated[list[str] | None, Field(
                            description="Optional subset of read-only permission names (see connection_status). "
                                        "Default: all supported read-only permissions; you can still decline "
                                        "any on the consent screen.")] = None,
                        timeout_seconds: Annotated[int, Field(ge=60, le=900)] = 300,
                        method: Annotated[Literal["auto", "ghealth_cli", "browser"], Field(
                            description="Google: 'auto' reuses the ghealth CLI's existing credentials (as "
                                        "health-coach-app does) when present, otherwise opens the browser")] = "auto"
                        ) -> dict[str, Any]:
        """Connect a provider and verify it with a read-only API call. Google reuses the `ghealth` CLI's OAuth
        credentials when available; otherwise the system browser opens (OAuth with PKCE + state, loopback on
        127.0.0.1). Returns a job_id; poll job_status until it succeeds (only then is the account verified)."""
        return _wrap(_connect)(app, provider, permissions, timeout_seconds, method)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                          open_world_hint=True))
    def disconnect_account(provider: Provider,
                           revoke_remote_authorization: bool = False,
                           delete_local_data: Annotated[bool, Field(description="Also delete this provider's "
                                                                               "imported records and raw files")]
                           = False,
                           include_takeout_data: bool = False,
                           confirmation_token: Annotated[str | None, Field(
                               description="Only after the user explicitly agreed to the actions returned by a "
                                           "first call without this token")] = None) -> dict[str, Any]:
        """Remove local tokens. Optionally revoke the app's authorization at the provider and/or delete local data.
        Imported data is kept unless delete_local_data is true. Revocation or deletion requires a two-step
        confirmation: call once to get a confirmation_token, ask the user, then call again with it."""
        return _wrap(_disconnect)(app, provider, revoke_remote_authorization, delete_local_data,
                                  include_takeout_data, confirmation_token)

    # -------------------------------------------------------------------- imports
    @mcp.tool(annotations=LOCAL_WRITE)
    def start_import(provider: Provider | None = None, start_date: DateStr | None = None,
                     end_date: DateStr | None = None,
                     categories: Annotated[list[Category] | None, Field(description="Default: all categories the "
                                                                                    "provider supports")] = None,
                     include_intraday: Annotated[bool, Field(description="Fitbit only: minute/second samples "
                                                                         "(10 requests per day of history)")] = False,
                     full_heart_rate: Annotated[bool, Field(description="Google only: whole-day heart rate "
                                                                        "(~37 MB/day) instead of exercise windows")]
                     = False,
                     include_unverified_types: Annotated[bool, Field(
                         description="Google only: include the 21 types defined by Google's API description but not "
                                     "yet confirmed against a live account (nutrition, ECG, IRN, moods, symptoms, "
                                     "reproductive health, body fat, ...). Default true; false limits the run to "
                                     "the 23 live-verified types.")] = True
                     ) -> dict[str, Any]:
        """Import history into the local database as a background job. Without dates: all API-accessible history
        (Fitbit from the account's memberSince date; Google: each type's first day is discovered by walking back
        until the data runs out). Returns a job_id."""
        return _wrap(_start_import)(app, provider, start_date, end_date, categories, include_intraday,
                                    full_heart_rate, include_unverified_types)

    @mcp.tool(annotations=LOCAL_WRITE)
    def sync_data(provider: Provider | None = None,
                  overlap_days: Annotated[int, Field(ge=1, le=90, description="Days before the last sync to "
                                                                              "re-fetch for late or edited data")] = 3,
                  categories: list[Category] | None = None, include_intraday: bool = False) -> dict[str, Any]:
        """Fetch new and recently changed records: re-requests the overlap window (never served from cache),
        updates changed records, de-duplicates by provider IDs and flags records the provider no longer returns
        as deleted_upstream. Returns a job_id."""
        return _wrap(_sync)(app, provider, overlap_days, categories, include_intraday)

    @mcp.tool(annotations=LOCAL_WRITE)
    def import_takeout(path: Annotated[str, Field(description="Local path to a Takeout/Fitbit export .zip or "
                                                              ".tgz, a folder of archive parts, or an extracted "
                                                              "folder")]) -> dict[str, Any]:
        """Import a downloaded Google Takeout / Fitbit data export (use when API access is unavailable). Extracts
        safely with size and path checks, detects supported files by content, and reports unsupported ones."""
        return _wrap(_takeout)(app, path)

    # ----------------------------------------------------------------------- jobs
    @mcp.tool(annotations=READ_ONLY)
    def job_status(job_id: str | None = None, limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, Any]:
        """Status and progress of one job, or the most recent jobs when job_id is omitted."""
        if job_id:
            return _job_view(app.jobs.get(job_id))
        return {"jobs": [_job_view(j) for j in app.jobs.list(limit)]}

    @mcp.tool(annotations=LOCAL_WRITE)
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Request cancellation; the job stops at the next safe point (including during rate-limit waits)."""
        return _wrap(lambda: _job_view(app.jobs.cancel(job_id)))()

    @mcp.tool(annotations=LOCAL_WRITE)
    def resume_job(job_id: str) -> dict[str, Any]:
        """Resume an interrupted, cancelled, failed or partial import/sync/takeout job. Completed requests are
        skipped; failed ones are retried."""
        return _wrap(lambda: _job_view(app.jobs.resume(job_id)))()

    # ----------------------------------------------------------------------- data
    @mcp.tool(annotations=READ_ONLY)
    def list_data_types() -> dict[str, Any]:
        """Local categories and metrics with record counts, date coverage and units, plus categories that are
        missing, denied, not granted or failed, and file/response formats that could not be parsed."""
        return queries.data_types(app.store)

    @mcp.tool(annotations=READ_ONLY)
    def query_data(category: str | None = None, metric: str | None = None, start_date: DateStr | None = None,
                   end_date: DateStr | None = None,
                   provider: Literal["fitbit", "google", "fitbit_takeout", "google_takeout"] | None = None,
                   granularity: Literal["sample", "interval", "session", "daily"] | None = None,
                   min_value: float | None = None, max_value: float | None = None,
                   include_deleted: bool = False, include_original: bool = False,
                   order: Literal["asc", "desc"] = "asc",
                   limit: Annotated[int, Field(ge=1, le=queries.MAX_QUERY_LIMIT)] = 100,
                   cursor: str | None = None) -> dict[str, Any]:
        """Filter local records (dates are inclusive calendar dates). Paginate with next_cursor."""
        return _wrap(queries.query)(app.store, category=category, metric=metric, start_date=start_date,
                                    end_date=end_date, provider=provider, granularity=granularity,
                                    min_value=min_value, max_value=max_value, include_deleted=include_deleted,
                                    include_original=include_original, order=order, limit=limit, cursor=cursor)

    @mcp.tool(annotations=READ_ONLY)
    def summarize_data(metric: Literal["steps", "distance", "calories_out", "sleep", "resting_heart_rate", "hrv",
                                       "heart_rate", "weight"],
                       start_date: DateStr | None = None, end_date: DateStr | None = None,
                       period: Literal["day", "week", "month"] = "week",
                       provider: Literal["fitbit", "google", "fitbit_takeout", "google_takeout"] | None = None
                       ) -> dict[str, Any]:
        """Descriptive statistics and trend for one metric, with its definition, coverage and exclusions.
        Default range: the 30 days ending at the latest local data."""
        return _wrap(summaries.summarize)(app.store, metric, start_date, end_date, period, provider,
                                          app.settings.today())

    @mcp.tool(annotations=LOCAL_WRITE)
    def export_data(format: Literal["json", "csv"] = "csv", category: str | None = None, metric: str | None = None,
                    start_date: DateStr | None = None, end_date: DateStr | None = None,
                    provider: Literal["fitbit", "google", "fitbit_takeout", "google_takeout"] | None = None,
                    granularity: Literal["sample", "interval", "session", "daily"] | None = None,
                    include_deleted: bool = False, include_original: bool = False) -> dict[str, Any]:
        """Write matching local records to a JSON or CSV file in the local exports folder; returns its path."""
        return _wrap(queries.export)(app.store, app.settings, fmt=format, include_original=include_original,
                                     category=category, metric=metric, start_date=start_date, end_date=end_date,
                                     provider=provider, granularity=granularity, include_deleted=include_deleted)

    mcp.app = app  # type: ignore[attr-defined]
    return mcp


# ------------------------------------------------------------------ implementations

def _provider_state(app: App, provider: str, check_remote: bool) -> dict[str, Any]:
    settings = app.settings
    state: dict[str, Any] = {"provider": provider}
    try:
        if provider == "fitbit":
            state["configured"] = bool(settings.fitbit_client_id())
            days_left = (settings.fitbit_shutdown() - settings.today()).days
            state["api_available"] = days_left > 0
            state["availability"] = ("Legacy Fitbit Web API is scheduled to be turned off on {} ({} day(s) left). "
                                     "Use an existing working Fitbit developer app only.".format(
                                         settings.fitbit_shutdown().isoformat(), max(days_left, 0)))
            all_scopes = catalog.FITBIT_SCOPES
        else:
            state["configured"] = settings.google_client() is not None
            state["client_secret_path"] = str(settings.google_client_secrets_path())
            cli = read_ghealth_credentials(settings)
            state["ghealth_cli_credentials"] = ("found at {} (used for connect_account, read-only)".format(
                settings.ghealth_credentials_path()) if cli else "not found")
            state["api_available"] = True
            state["availability"] = ("Uses your existing Google Health API project (the ghealth CLI setup shared "
                                     "with health-coach-app). Google is not onboarding new projects. While the "
                                     "consent screen is in Testing mode, refresh tokens expire after 7 days: run "
                                     "`ghealth auth login` and the server picks up the new credentials.")
            state["timezone_for_day_boundaries"] = str(settings.timezone())
            all_scopes = catalog.GOOGLE_SCOPES
    except ConfigError as error:
        state["configured"], state["config_error"] = False, str(error)
        all_scopes = []
    token = TokenStore(settings, provider).load()
    if not token:
        state["token_status"] = "none"
        state["connected"] = False
    else:
        if token.get("status") == "invalid":
            state["token_status"] = "invalid_reconnect_required"
        elif token.get("expires_at", 0) > time.time() + 60:
            state["token_status"] = "active"
        else:
            state["token_status"] = "access_expired_refresh_available" if token.get("refresh_token") else "expired"
        granted = token.get("scope", "").split()
        state["granted_permissions"] = sorted(_short_scope(s) for s in granted)
        state["not_granted"] = sorted(_short_scope(s) for s in all_scopes if s not in granted)
        if provider == "google" and state["not_granted"]:
            state["how_to_grant_missing"] = (
                "1) In Google Cloud console > Google Auth Platform > Data Access for the project behind "
                "~/.config/ghealth/client_secret.json, add the googlehealth.<name>.readonly scopes listed in "
                "not_granted. 2) Call connect_account with provider 'google' and method 'browser' and approve "
                "them. Data types needing a missing permission are recorded as scope_not_granted, not as empty.")
        state["connected_since"] = token.get("obtained_at")
        if token.get("seeded_from"):
            state["credential_source"] = token["seeded_from"]
        verified = app.importer.get_meta("verified:" + provider)
        state["last_verification"] = verified
        state["connected"] = bool(verified and verified.get("ok")) and state["token_status"] != \
            "invalid_reconnect_required"
        if check_remote and state["token_status"] != "invalid_reconnect_required":
            state["remote_check"] = _remote_check(app, provider)
            state["connected"] = state["remote_check"].get("ok", False)
    state["active_jobs"] = [j["id"] for j in app.jobs.active(provider=provider)]
    return state


def _remote_check(app: App, provider: str) -> dict[str, Any]:
    def no_wait(seconds, reason):
        raise RateLimitExhausted(seconds)

    session = OAuthSession(app.settings, provider, **({"http": app.http} if app.http else {}))
    client = ApiClient(session, waiter=no_wait, http=app.http, wait=False)
    try:
        if provider == "google":
            identity = client.get("/v4/users/me/identity")
            ok = bool(identity.get("healthUserId"))
            return {"ok": ok, "checked": "users.getIdentity", **({} if ok else {"reason": "account not linked"})}
        path = "/1/user/-/profile.json" if "profile" in session.granted_scopes() else "/1/user/-/devices.json"
        client.get(path)
        return {"ok": True, "checked": path}
    except ApiError as error:
        return {"ok": False, "reason": "HTTP {}: {}".format(error.status, error.reason)}
    except RateLimitExhausted:
        return {"ok": None, "reason": "rate limited; try again later"}
    except NetworkError:
        return {"ok": None, "reason": "network error"}
    except AuthError as error:
        return {"ok": False, "reason": redact(error)}


def _connection_status(app: App, provider: str, check_remote: bool) -> dict[str, Any]:
    providers = ["google", "fitbit"] if provider == "all" else [provider]
    result: dict[str, Any] = {"providers": [_provider_state(app, p, check_remote) for p in providers],
                              "data_home": str(app.settings.home),
                              "takeout_fallback": "import_takeout works without API access."}
    warning = app.settings.inside_project_warning()
    if warning:
        result["warning"] = warning
    connected = [p["provider"] for p in result["providers"] if p.get("connected")]
    result["summary"] = ("Connected: " + ", ".join(connected)) if connected else "No verified API connection."
    return result


def _has_credentials(app: App, provider: str) -> bool:
    if TokenStore(app.settings, provider).load().get("refresh_token"):
        return True
    return provider == "google" and read_ghealth_credentials(app.settings) is not None


def _default_provider(app: App, provider: str | None, need_token: bool) -> str:
    if provider:
        return provider
    settings = app.settings
    candidates = []
    try:
        if settings.google_client() is not None:
            candidates.append("google")
    except ConfigError:
        pass
    if settings.fitbit_client_id() and settings.today() < settings.fitbit_shutdown():
        candidates.append("fitbit")
    if need_token:
        candidates = [p for p in candidates if _has_credentials(app, p)]
    if not candidates:
        raise ToolError("No usable API provider is configured{}. Configure an approved Google Health API project "
                        "or an existing Fitbit app (see README), or use import_takeout."
                        .format(" and connected" if need_token else ""))
    return candidates[0]


def _connect(app: App, provider: str | None, permissions: list[str] | None, timeout_seconds: int,
             method: str = "auto") -> dict:
    provider = _default_provider(app, provider, need_token=False)
    if provider == "google":
        available = {s: catalog.google_scope(s) for s in catalog.GOOGLE_SCOPE_NAMES}
    else:
        available = {s: s for s in catalog.FITBIT_SCOPES}
    # Default request: every read-only data permission. `location` (exercise GPS) is not used by the importer,
    # so it is only requested when named explicitly.
    names = permissions or sorted(n for n in available if n != "location")
    unknown = sorted(set(names) - set(available))
    if unknown:
        raise ToolError("Unknown permission(s): {}. Valid: {}".format(", ".join(unknown), ", ".join(sorted(available))))
    from .oauth import check_fitbit_available, provider_client
    if provider == "fitbit":
        if method == "ghealth_cli":
            raise ToolError("The ghealth CLI holds Google credentials; use provider 'google'.")
        check_fitbit_available(app.settings)
        method = "browser"
    elif method == "auto":
        method = "ghealth_cli" if read_ghealth_credentials(app.settings) and not permissions else "browser"
    if method == "ghealth_cli" and read_ghealth_credentials(app.settings) is None:
        raise ToolError("No ghealth CLI credentials found at {}. Run `ghealth auth login` or use method "
                        "'browser'.".format(app.settings.ghealth_credentials_path()))
    provider_client(app.settings, provider)  # fail fast with setup instructions
    existing = app.jobs.active(kind="connect", provider=provider)
    if existing:
        return dict(_job_view(existing[0]), note="A connection attempt is already in progress.")
    job = app.jobs.submit("connect", provider, {"scopes": [available[n] for n in names], "method": method,
                                                "timeout_seconds": timeout_seconds}, lane="connect:" + provider)
    if method == "ghealth_cli":
        hint = ("Reusing the ghealth CLI's existing Google credentials (no browser). Poll job_status; success "
                "means a read-only API call worked.")
    else:
        hint = ("Complete sign-in in the browser window, then poll job_status. If no browser opened, job_status "
                "shows the sign-in URL.")
    return dict(_job_view(job), method=method, next_step=hint)


def _start_import(app: App, provider, start_date, end_date, categories, include_intraday,
                  full_heart_rate: bool = False, include_unverified_types: bool = True) -> dict:
    provider = _default_provider(app, provider, need_token=True)
    valid = catalog.FITBIT_CATEGORIES if provider == "fitbit" else catalog.GOOGLE_CATEGORIES
    if categories:
        bad = sorted(set(categories) - set(valid))
        if bad:
            raise ToolError("Not available from {}: {}. Valid: {}".format(provider, ", ".join(bad), ", ".join(valid)))
    if include_intraday and provider != "fitbit":
        raise ToolError("include_intraday applies to Fitbit only; Google returns full-resolution records anyway.")
    if full_heart_rate and provider != "google":
        raise ToolError("full_heart_rate applies to Google only.")
    if (end_date and not start_date) and provider == "google":
        raise ToolError("Give start_date with end_date (or neither for all history).")
    from .util import parse_date
    s, e = parse_date(start_date, "start_date"), parse_date(end_date, "end_date")
    if s and e and s > e:
        raise ToolError("start_date must be on or before end_date.")
    if e and e > app.settings.today():
        raise ToolError("end_date cannot be in the future.")
    if not _has_credentials(app, provider):
        raise ToolError("Not connected to {}. Run connect_account first.".format(provider))
    busy = [j for j in app.jobs.active(provider=provider) if j["kind"] in ("import", "sync")]
    params = {"mode": "import", "start_date": start_date, "end_date": end_date,
              "categories": sorted(categories) if categories else None, "include_intraday": include_intraday,
              "full_heart_rate": full_heart_rate, "include_unverified": include_unverified_types}
    job = app.jobs.submit("import", provider, params, lane="api:" + provider)
    view = _job_view(job)
    if busy:
        view["note"] = "Queued behind {} (one API job per provider runs at a time).".format(busy[0]["id"])
    if provider == "fitbit":
        view["quota_note"] = ("Legacy Fitbit allows about 150 requests/hour; long histories may wait at rate limits. "
                              "The job waits automatically and can be cancelled or resumed.")
    return view


def _sync(app: App, provider, overlap_days, categories, include_intraday) -> dict:
    provider = _default_provider(app, provider, need_token=True)
    valid = catalog.FITBIT_CATEGORIES if provider == "fitbit" else catalog.GOOGLE_CATEGORIES
    if categories and set(categories) - set(valid):
        raise ToolError("Not available from {}: {}".format(provider, ", ".join(sorted(set(categories) - set(valid)))))
    if include_intraday and provider != "fitbit":
        raise ToolError("include_intraday applies to Fitbit only.")
    with app.store.connect() as conn:
        has_data = conn.execute("SELECT 1 FROM records WHERE provider=? LIMIT 1", (provider,)).fetchone() or \
            conn.execute("SELECT 1 FROM sync_state WHERE provider=?", (provider,)).fetchone()
    if not has_data:
        raise ToolError("Nothing imported from {} yet; run start_import first.".format(provider))
    job = app.jobs.submit("sync", provider, {"mode": "sync", "overlap_days": overlap_days,
                                             "categories": sorted(categories) if categories else None,
                                             "include_intraday": include_intraday}, lane="api:" + provider)
    return _job_view(job)


def _takeout(app: App, path: str) -> dict:
    source = Path(path).expanduser()
    if not source.is_absolute():
        raise ToolError("Give an absolute path (or one starting with ~).")
    if not source.exists():
        raise ToolError("Path not found on this computer: {}".format(source))
    try:
        source.resolve().relative_to(app.settings.home.resolve())
        raise ToolError("Choose a file outside the data folder.")
    except ValueError:
        pass
    job = app.jobs.submit("takeout", None, {"path": str(source)}, lane="takeout")
    return _job_view(job)


def _disconnect(app: App, provider: str, revoke_remote: bool, delete_local: bool, include_takeout: bool,
                token: str | None) -> dict:
    actions = ["Remove locally stored {} tokens".format(provider), "Cancel active {} jobs".format(provider)]
    if revoke_remote:
        actions.append("Revoke this app's authorization at {} (remote)".format(provider))
    if delete_local:
        actions.append("Permanently delete local {} records and retained raw responses".format(provider))
        if include_takeout:
            actions.append("Also delete {}_takeout records".format(provider))
    else:
        actions.append("Keep all imported data")
    needs_confirmation = revoke_remote or delete_local
    fingerprint = hashlib.sha256("{}|{}|{}|{}".format(provider, revoke_remote, delete_local,
                                                      include_takeout).encode()).hexdigest()
    if needs_confirmation:
        with app.confirm_lock:
            pending = app.confirmations
            now = time.time()
            for key in [k for k, v in pending.items() if v[1] < now]:
                pending.pop(key)
            if not token:
                new = secrets.token_urlsafe(16)
                pending[new] = (fingerprint, now + 300)
                return {"confirmation_required": True, "actions": actions, "confirmation_token": new,
                        "expires_in_seconds": 300,
                        "instruction": "Show these actions to the user. Only if they explicitly confirm, call "
                                       "disconnect_account again with the same arguments and this token."}
            entry = pending.pop(token, None)
            if entry is None or entry[0] != fingerprint:
                raise ToolError("Invalid or expired confirmation_token for these arguments; request a new one.")
    for job in app.jobs.active(provider=provider):
        app.jobs.cancel(job["id"])
    result: dict[str, Any] = {"provider": provider, "actions_performed": []}
    if revoke_remote:
        result["revocation"] = revoke(app.settings, provider, **({"http": app.http} if app.http else {}))
        result["actions_performed"].append("revocation attempted")
    removed = TokenStore(app.settings, provider).delete()
    result["actions_performed"].append("local tokens removed" if removed else "no local tokens were stored")
    app.importer.set_meta("verified:" + provider, None)
    if delete_local:
        targets = [provider] + ([provider + "_takeout"] if include_takeout else [])
        result["deleted"] = app.store.delete_provider_data(targets)
        result["actions_performed"].append("local data deleted")
    else:
        result["data_kept"] = True
    if revoke_remote and not result["revocation"].get("revoked"):
        result["manual_step"] = ("Revocation was not confirmed by the provider. Remove access manually in your "
                                 "Google Account (Security > Third-party connections) or Fitbit account settings.")
    return result


def serve_stdio(server: MCPServer) -> None:
    """Serve MCP on a private duplicate of stdout and point fd 1 at stderr for the whole process lifetime.

    The SDK already diverts fd 1 while serving, but restores it on exit, so text a library buffered in
    sys.stdout could still be flushed onto the wire at interpreter shutdown. Here nothing but the
    transport ever holds the real stdout.
    """
    import io

    import anyio
    from mcp.server.stdio import stdio_server

    lowlevel = getattr(server, "_lowlevel_server", None)
    if lowlevel is None:  # pragma: no cover - SDK internals changed; fall back to the SDK's own diversion
        server.run()
        return
    sys.stdout.flush()
    wire_fd = os.dup(1)
    os.dup2(2, 1)
    wire = anyio.wrap_file(io.TextIOWrapper(os.fdopen(wire_fd, "wb"), encoding="utf-8", newline="\n"))

    async def run() -> None:
        async with stdio_server(stdout=wire) as (read_stream, write_stream):
            await lowlevel.run(read_stream, write_stream, lowlevel.create_initialization_options())

    anyio.run(run)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.logs_dir, os.environ.get("FITBIT_MCP_LOG_LEVEL", "INFO"))
    log.info("fitbit-mcp starting (pid %s); data home %s", os.getpid(), settings.home)
    serve_stdio(create_server(build_app(settings)))


if __name__ == "__main__":
    main()
