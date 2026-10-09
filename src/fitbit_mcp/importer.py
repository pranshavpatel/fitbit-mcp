"""Job runners: connect, API import, sync and Takeout import.

Import and sync differ deliberately from the original exporter's resume cache:
* every job owns a persisted request plan (job_requests); resume skips completed requests
  of *that job* only;
* sync builds a fresh plan for an overlap window, so recent days are always re-fetched;
* results are upserted by stable provider IDs (Google DataPoint name, Fitbit logId, or
  calendar date for daily series), so re-fetches update records instead of duplicating;
* when a request covering a scope (one category on one date) completes, records in that
  scope that the provider no longer returns are marked deleted_upstream (kept, not erased).
"""
from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import catalog
from . import google_registry as reg
from .client import ApiClient, ApiError, checked_url
from .config import Settings
from .jobs import JobContext
from .oauth import AuthError, OAuthSession, check_fitbit_available, run_login, seed_google_from_ghealth
from .parsers import fitbit as fitbit_parser
from .parsers import google as google_parser
from .parsers.base import ParseResult
from .store import Store
from .takeout import Limits, TakeoutError, extract, iter_files, parse_file
from .util import canonical_json, chunks, daterange, parse_date, parse_rfc3339, sha256_bytes, utcnow

FITBIT_REQUESTS_PER_HOUR = 150
MAX_PAGES_PER_REQUEST = 500   # guard against a pathological paging loop (as in health-coach-app)


class Importer:
    def __init__(self, settings: Settings, store: Store, http=None, open_browser=None):
        self.settings, self.store, self.http, self.open_browser = settings, store, http, open_browser
        self.last_query: dict = {}

    # ------------------------------------------------------------------ helpers
    def session(self, provider: str) -> OAuthSession:
        kwargs = {"http": self.http} if self.http is not None else {}
        return OAuthSession(self.settings, provider, **kwargs)

    def client(self, provider: str, ctx: JobContext) -> ApiClient:
        return ApiClient(self.session(provider), waiter=ctx.wait, http=self.http)

    def set_meta(self, key: str, value: Any) -> None:
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value=excluded.value", (key, json.dumps(value)))

    def get_meta(self, key: str) -> Any:
        with self.store.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    # ------------------------------------------------------------------ connect
    def run_connect(self, ctx: JobContext) -> dict:
        provider = ctx.provider
        scopes = ctx.params["scopes"]
        if ctx.params.get("method") == "ghealth_cli":
            ctx.progress(force=True, stage="importing_ghealth_cli_credentials")
            seed_google_from_ghealth(self.settings)
            self.session("google").bearer()   # refreshes now if needed, so a dead token fails here
            granted = sorted(self.session("google").granted_scopes())
            summary = {"granted_scopes": granted, "denied_scopes": sorted(set(scopes) - set(granted)),
                       "credential_source": "ghealth CLI credentials"}
            ctx.progress(force=True, stage="validating_api_access", **summary)
            verification = self.verify(provider, ctx)
            if not verification["ok"]:
                raise AuthError("The ghealth credentials loaded, but a read-only test request failed ({})."
                                .format(verification["reason"]))
            ctx.progress(force=True, stage="connected", **summary)
            return {"status": "succeeded", "message": "Connected with the ghealth CLI's credentials and verified "
                                                      "with a read-only API request.",
                    "verified_with": verification["check"]}
        kwargs = {"open_browser": self.open_browser} if self.open_browser else {}
        if self.http is not None:
            kwargs["http"] = self.http
        summary = run_login(self.settings, provider, scopes, cancel=ctx.cancel_event,
                            report=lambda info: ctx.progress(force=True, **info),
                            timeout=float(ctx.params.get("timeout_seconds", 300)), **kwargs)
        ctx.progress(force=True, stage="validating_api_access", authorization_url=None, **summary)
        verification = self.verify(provider, ctx)
        if not verification["ok"]:
            raise AuthError("Authorization completed, but the API rejected a read-only test request ({}). "
                            "Tokens are stored but the connection is not usable yet.".format(verification["reason"]))
        ctx.progress(force=True, stage="connected", **summary)
        return {"status": "succeeded", "message": "Connected and verified with a read-only API request.",
                "verified_with": verification["check"]}

    def verify(self, provider: str, ctx: JobContext) -> dict:
        client = self.client(provider, ctx)
        granted = self.session(provider).granted_scopes()
        try:
            if provider == "google":
                identity = client.get("/v4/users/me/identity")
                self.store.save_raw("google", "api", "identity", "/v4/users/me/identity", None,
                                    json.dumps(identity).encode(), ctx.id)
                if not identity.get("healthUserId"):
                    result = {"ok": False, "check": "identity", "reason": "no linked Google Health identity "
                                                                          "(ACCOUNT_NOT_LINKED)"}
                else:
                    result = {"ok": True, "check": "users.getIdentity",
                              "account_fingerprint": sha256_bytes(identity["healthUserId"].encode())[:12]}
            else:
                if "profile" in granted:
                    path, check = "/1/user/-/profile.json", "profile"
                else:
                    today = self.settings.today().isoformat()
                    path, check = next(((p.format(start=today, end=today), g) for g, _, s, p, _ in catalog.FITBIT_RANGES
                                        if s in granted), (None, None))
                    if path is None:
                        return {"ok": False, "check": "none", "reason": "no data scopes were granted"}
                client.get(path)
                user = self.session(provider).token().get("user_id") or ""
                result = {"ok": True, "check": check,
                          "account_fingerprint": sha256_bytes(str(user).encode())[:12] if user else None}
        except ApiError as error:
            result = {"ok": False, "check": "api", "reason": "HTTP {}: {}".format(error.status, error.reason)}
        self.set_meta("verified:" + provider, dict(result, at=utcnow()))
        return result

    # ---------------------------------------------------------------- planning
    def _plan_exists(self, job_id: str) -> bool:
        with self.store.connect() as conn:
            return conn.execute("SELECT 1 FROM job_requests WHERE job_id=? LIMIT 1", (job_id,)).fetchone() is not None

    def _save_plan(self, job_id: str, rows: list[dict]) -> None:
        with self.store.transaction() as conn:
            for seq, row in enumerate(rows):
                key = sha256_bytes(canonical_json([row["grp"], row["path"], row["params"], row.get("window_start"),
                                                   row.get("window_end")]).encode())[:24]
                conn.execute("INSERT OR IGNORE INTO job_requests(job_id, seq, key, grp, category, scope, path, params,"
                             " window_start, window_end, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (job_id, seq, key, row["grp"], row.get("category"), row.get("scope"), row["path"],
                              json.dumps(row["params"]), row.get("window_start"), row.get("window_end"), utcnow()))

    def _window(self, ctx: JobContext, provider: str) -> tuple[date | None, date | None]:
        params = ctx.params
        today = self.settings.today()
        if params.get("mode") == "sync":
            if params.get("window_start"):
                return parse_date(params["window_start"]), parse_date(params["window_end"])
            overlap = int(params.get("overlap_days", 7))
            with self.store.connect() as conn:
                state = conn.execute("SELECT last_synced_through FROM sync_state WHERE provider=?",
                                     (provider,)).fetchone()
                latest = conn.execute("SELECT MAX(local_date) AS d FROM records WHERE provider=? AND "
                                      "status='active' AND local_date <= ?", (provider, today.isoformat())).fetchone()
            anchor = None
            if state and state["last_synced_through"]:
                anchor = date.fromisoformat(state["last_synced_through"])
            elif latest and latest["d"]:
                anchor = date.fromisoformat(latest["d"])
            if anchor is None:
                raise AuthError("No previous import for {}; run start_import first.".format(provider))
            start = min(anchor, today) - timedelta(days=overlap)
            params.update(window_start=start.isoformat(), window_end=today.isoformat())
            ctx.manager._update(ctx.id, params=json.dumps(params))
            return start, today
        start, end = parse_date(params.get("start_date"), "start_date"), parse_date(params.get("end_date"), "end_date")
        return start, end

    def _plan_fitbit(self, ctx: JobContext, client: ApiClient) -> None:
        params = ctx.params
        categories = set(params.get("categories") or catalog.FITBIT_CATEGORIES)
        granted = self.session("fitbit").granted_scopes()
        start, end = self._window(ctx, "fitbit")
        end = end or self.settings.today()
        sync = params.get("mode") == "sync"
        rows: list[dict] = []
        if not sync:
            for name, scope, path in catalog.FITBIT_SNAPSHOTS:
                rows.append(dict(grp="snapshot-" + name, category="snapshot", scope=scope, path=path, params={}))
        if start is None:
            if "profile" not in granted:
                raise AuthError("Specify start_date, or grant the 'profile' permission so the account start "
                                "date (memberSince) can be detected.")
            profile = client.get("/1/user/-/profile.json")
            member_since = (profile.get("user") or {}).get("memberSince")
            if not member_since:
                raise AuthError("Profile has no memberSince; specify start_date.")
            start = date.fromisoformat(member_since[:10])
            params["start_date"] = start.isoformat()
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        params.setdefault("end_date", end.isoformat())
        ranged = []
        for grp, category, scope, template, max_days in catalog.FITBIT_RANGES:
            if category not in categories:
                continue
            for s, e in chunks(start, end, max_days):
                ranged.append(dict(grp=grp, category=category, scope=scope,
                                   path=template.format(start=s.isoformat(), end=e.isoformat()), params={},
                                   window_start=s.isoformat(), window_end=e.isoformat()))
        ranged.sort(key=lambda r: r["window_end"], reverse=True)  # newest data first, across categories
        rows += ranged
        for grp, category, scope, path, limit in catalog.FITBIT_LISTS:
            if category not in categories:
                continue
            list_params = ({"afterDate": start.isoformat(), "sort": "asc", "offset": 0, "limit": limit} if sync else
                           {"beforeDate": (end + timedelta(days=1)).isoformat(), "sort": "desc", "offset": 0,
                            "limit": limit})
            rows.append(dict(grp=grp, category=category, scope=scope, path=path, params=list_params))
        if params.get("include_intraday"):
            for day in sorted(daterange(start, end), reverse=True):
                for grp, category, scope, template in catalog.FITBIT_INTRADAY:
                    if category in categories:
                        rows.append(dict(grp=grp, category=category, scope=scope,
                                         path=template.format(date=day.isoformat()), params={},
                                         window_start=day.isoformat(), window_end=day.isoformat()))
        self._save_plan(ctx.id, rows)

    def _plan_google(self, ctx: JobContext, client: ApiClient) -> None:
        """Chunked, per-type plan (ported from health-coach-app's ingest/backfill)."""
        params = ctx.params
        specs = reg.specs_for(params.get("categories"), bool(params.get("include_unverified")),
                              bool(params.get("full_heart_rate")))
        tz = self.settings.timezone()
        start, end = self._window(ctx, "google")
        end = end or self.settings.today()
        params.setdefault("end_date", end.isoformat())
        params["timezone"] = str(tz)
        if start and start > end:
            raise ValueError("start_date must be on or before end_date")
        identity = client.get("/v4/users/me/identity")
        if not identity.get("healthUserId"):
            raise AuthError("No linked Google Health identity. Sign into the Fitbit / Google Health app with this "
                            "Google account, then connect again.")
        self.store.save_raw("google", "api", "identity", "/v4/users/me/identity", None,
                            json.dumps(identity).encode(), ctx.id)
        rows: list[dict] = []
        if params.get("mode") != "sync":
            for name, scope in (("profile", "profile"), ("settings", "settings"), ("irnProfile", "irn")):
                rows.append(dict(grp="snapshot-" + name, category="snapshot", scope=catalog.google_scope(scope),
                                 path="/v4/users/me/" + name, params={}))
            rows.append(dict(grp="snapshot-pairedDevices", category="snapshot", scope=None,
                             path="/v4/users/me/pairedDevices", params={}))
        floor = self.settings.history_floor()
        ranged, exercise_scoped, origins = [], [], {}
        for spec in specs:
            scope = catalog.google_scope(spec.scope_name)
            base = "/v4/users/me/dataTypes/" + spec.id + "/dataPoints"
            if spec.scope == reg.SCOPE_EXERCISE:
                exercise_scoped.append(dict(grp=spec.id, category=spec.category, scope=scope, path=base,
                                            params={"op": "exercise_windows", "pageSize": spec.page_size},
                                            window_start=start.isoformat() if start else None,
                                            window_end=end.isoformat()))
                continue
            if spec.filter_kind == reg.NO_FILTER:   # reference entities (food, units): one unfiltered request
                ranged.append(dict(grp=spec.id, category=spec.category, scope=scope, path=base,
                                   params={"op": "list", "pageSize": spec.page_size}, window_start=None,
                                   window_end=None, sort_key="9999"))
                continue
            spec_start = start
            if spec_start is None:
                if scope not in self.session("google").granted_scopes():
                    spec_start = end  # one row, reported as "not granted" at execution
                else:
                    ctx.message("Discovering how far back {} goes".format(spec.id))
                    try:
                        spec_start = self._origin(ctx, client, spec, end, tz, floor)
                    except ApiError:
                        spec_start = end  # one row, so the provider's error is recorded at execution
                    origins[spec.id] = spec_start.isoformat() if spec_start else None
                    if spec_start is None:
                        self.store.set_category_status("google", spec.id, spec.category, "empty",
                                                       "No data found by history discovery", ctx.id)
                        continue
            if floor and spec_start < floor:
                spec_start = floor
            for chunk_start, chunk_end in chunks(spec_start, end, spec.max_chunk_days):
                if spec.operation == reg.DAILY_ROLLUP:
                    row_params = {"op": "rollup"}
                    path = base + ":dailyRollUp"
                else:
                    # The filter is built at execution time, so a fallback form found for one chunk
                    # (unverified types) is reused by every later chunk.
                    row_params = {"op": "list", "pageSize": spec.page_size}
                    path = base
                ranged.append(dict(grp=spec.id, category=spec.category, scope=scope, path=path, params=row_params,
                                   window_start=chunk_start.isoformat(), window_end=chunk_end.isoformat()))
        ranged.sort(key=lambda r: r.get("sort_key") or r["window_end"], reverse=True)   # newest history first
        if origins:
            ctx.progress(force=True, discovered_history_start=origins)
        self._save_plan(ctx.id, rows + ranged + exercise_scoped)

    def _days_with_data(self, client: ApiClient, spec: reg.Spec, start: date, end: date, tz,
                        exhaustive: bool) -> set[str]:
        """An empty page is not "no data": keep paging while the server offers a token."""
        days: set[str] = set()
        token, seen = None, set()
        base = "/v4/users/me/dataTypes/" + spec.id + "/dataPoints"
        for _ in range(25 if exhaustive else 6):
            if spec.operation == reg.DAILY_ROLLUP:
                payload = client.rollup(base + ":dailyRollUp", reg.rollup_body(start, end, token))
            else:
                payload = self._list_page(None, client, spec, start, end, tz, token)
            days |= {r.local_date for r in google_parser.parse_page(spec.id, payload).records
                     if r.local_date and start.isoformat() <= r.local_date <= end.isoformat()}
            if days and not exhaustive:
                return days
            token = payload.get("nextPageToken") if isinstance(payload, dict) else None
            if not token or token in seen:
                return days
            seen.add(token)
        return days

    def _list_page(self, ctx: JobContext | None, client: ApiClient, spec: reg.Spec, start: date | None,
                   end: date | None, tz, token: str | None) -> Any:
        """One dataPoints.list page. For types not verified live, a 400 on the first page makes the importer
        try the other documented filter forms; the form that works is remembered for later chunks."""
        base = "/v4/users/me/dataTypes/" + spec.id + "/dataPoints"
        variants = reg.filter_variants(spec)
        cache_key = "filter:google:" + spec.id
        cached = self.get_meta(cache_key)
        order = list(range(len(variants)))
        if isinstance(cached, int) and 0 <= cached < len(variants):
            order.remove(cached)
            order.insert(0, cached)
        last_error = None
        for variant in order:
            query: dict[str, Any] = {"pageSize": spec.page_size}
            if start is not None and end is not None:
                flt = reg.build_filter(spec, start, end, tz, variant)
                if flt:
                    query["filter"] = flt
            if token:
                query["pageToken"] = token
            try:
                payload = client.get(base, params=query)
            except ApiError as error:
                if error.status != 400 or token or len(variants) == 1:
                    raise
                last_error = error
                continue
            if variant != cached and len(variants) > 1:
                self.set_meta(cache_key, variant)
                if ctx is not None and variant != 0:
                    ctx.warn("{}: used the alternative filter form '{}'".format(spec.id, variants[variant][0]))
            self.last_query = query
            return payload
        raise ApiError(400, "{}; none of the documented filter forms were accepted".format(
            last_error.reason if last_error else "INVALID_ARGUMENT"))

    def _origin(self, ctx: JobContext, client: ApiClient, spec: reg.Spec, end: date, tz,
                floor: date | None) -> date | None:
        """Walk back one window at a time until 3 consecutive windows are empty; cache the result."""
        key = "origin:google:" + spec.id
        cached = self.get_meta(key)
        if cached and not ctx.params.get("rediscover"):
            return date.fromisoformat(cached["origin"]) if cached.get("origin") else None
        window = min(30, spec.max_chunk_days)
        window_end, oldest, empty = end, None, 0
        for _ in range(80):
            ctx.checkpoint()
            window_start = window_end - timedelta(days=window - 1)
            if self._days_with_data(client, spec, window_start, window_end, tz, exhaustive=False):
                oldest, empty = (window_start, window_end), 0
            else:
                empty += 1
                if empty >= 3:
                    break
            window_end = window_start - timedelta(days=1)
            if floor and window_end < floor:
                break
        origin = None
        if oldest:
            days = self._days_with_data(client, spec, oldest[0], oldest[1], tz, exhaustive=True)
            origin = date.fromisoformat(min(days)) if days else oldest[0]
        self.set_meta(key, {"origin": origin.isoformat() if origin else None, "checked_at": utcnow()})
        return origin

    # --------------------------------------------------------------- execution
    def run_api(self, ctx: JobContext) -> dict:
        provider = ctx.provider
        if provider == "fitbit":
            check_fitbit_available(self.settings)
        client = self.client(provider, ctx)
        if not self._plan_exists(ctx.id):
            ctx.message("Planning requests")
            (self._plan_fitbit if provider == "fitbit" else self._plan_google)(ctx, client)
            ctx.manager._update(ctx.id, params=json.dumps(ctx.params))
        granted = self.session(provider).granted_scopes()
        totals = dict(ctx._progress.get("records") or {"inserted": 0, "updated": 0, "unchanged": 0, "restored": 0,
                                                          "marked_deleted_upstream": 0})
        while True:
            ctx.checkpoint()
            with self.store.connect() as conn:
                row = conn.execute("SELECT * FROM job_requests WHERE job_id=? AND status='pending' ORDER BY seq "
                                   "LIMIT 1", (ctx.id,)).fetchone()
                counts = {r["status"]: r["n"] for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM job_requests WHERE job_id=? GROUP BY status", (ctx.id,))}
            total = sum(counts.values())
            remaining = counts.get("pending", 0)
            eta = None
            if provider == "fitbit" and remaining:
                eta = round(remaining / FITBIT_REQUESTS_PER_HOUR, 1)
            ctx.progress(requests_total=total, requests_done=total - remaining, requests_by_status=counts,
                         records=totals, estimated_hours_remaining_at_quota=eta)
            if row is None:
                break
            self._execute(ctx, client, dict(row), granted, totals)
        with self.store.connect() as conn:
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM job_requests WHERE job_id=? GROUP BY status", (ctx.id,))}
        with self.store.transaction() as conn:
            data_issues = conn.execute("SELECT COUNT(*) FROM job_requests WHERE job_id=? AND category != 'snapshot' "
                                       "AND status IN ('error','skipped')", (ctx.id,)).fetchone()[0]
            data_errors = conn.execute("SELECT COUNT(*) FROM job_requests WHERE job_id=? AND category != 'snapshot' "
                                       "AND status = 'error' AND COALESCE(http_status, 0) NOT IN (401, 403)",
                                       (ctx.id,)).fetchone()[0]
            snapshot_issues = [dict(r) for r in conn.execute(
                "SELECT grp, status, reason FROM job_requests WHERE job_id=? AND category='snapshot' AND status IN "
                "('error','skipped')", (ctx.id,))]
            end = ctx.params.get("window_end") or ctx.params.get("end_date") or self.settings.today().isoformat()
            # Advance the sync watermark only when no data request failed transiently (permission denials are
            # persistent states, not gaps), so failed days are re-requested by the next sync.
            conn.execute("INSERT INTO sync_state(provider, last_synced_through, last_sync_at, last_job_id, "
                         "last_import_start) VALUES (?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                         "last_synced_through=CASE WHEN excluded.last_synced_through IS NULL THEN "
                         "sync_state.last_synced_through ELSE MAX(COALESCE(sync_state.last_synced_through,''), "
                         "excluded.last_synced_through) END, last_sync_at=excluded.last_sync_at, "
                         "last_job_id=excluded.last_job_id, last_import_start=COALESCE(excluded.last_import_start, "
                         "sync_state.last_import_start)",
                         (provider, None if data_errors else end, utcnow(), ctx.id, ctx.params.get("start_date")))
        ctx.progress(force=True, requests_by_status=counts, records=totals, estimated_hours_remaining_at_quota=None,
                     account_snapshots_not_retrieved=snapshot_issues)
        partial = data_issues > 0
        return {"status": "partial" if partial else "succeeded",
                "message": ("Completed. Some categories were denied, not granted, or failed; see list_data_types."
                            if partial else "Completed; every planned request succeeded.")}

    def _update_request(self, ctx: JobContext, key: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        allowed = ("status", "cursor", "pages", "records", "http_status", "reason", "params", "resumed_mid_pagination")
        names = [k for k in fields if k in allowed or k == "updated_at"]
        with self.store.transaction() as conn:
            conn.execute("UPDATE job_requests SET " + ", ".join(n + "=?" for n in names) + " WHERE job_id=? AND key=?",
                         [fields[n] for n in names] + [ctx.id, key])

    def _execute(self, ctx: JobContext, client: ApiClient, row: dict, granted: set[str], totals: dict) -> None:
        provider, grp, key = ctx.provider, row["grp"], row["key"]
        params = json.loads(row["params"])
        ctx.progress(force=True, current=grp, current_window=[row["window_start"], row["window_end"]],
                     current_pages=row["pages"], current_records=row["records"])
        if row["scope"] and row["scope"] not in granted:
            self._update_request(ctx, key, status="skipped", reason="Permission not granted: " + row["scope"])
            if row["category"] != "snapshot":
                self.store.set_category_status(provider, grp, row["category"], "scope_not_granted",
                                               "Permission not granted: " + row["scope"], ctx.id)
            return
        if params.get("op") == "exercise_windows":
            return self._execute_exercise_windows(ctx, client, row, params, totals)
        if provider == "google" and grp in reg.SPECS and reg.SPECS[grp].catalog:
            # plans made before catalog types were excluded: skip without fetching
            self._update_request(ctx, key, status="ok", reason="shared reference catalog; not imported")
            return
        cursor = row["cursor"]
        spec = reg.SPECS.get(grp) if provider == "google" else None
        # Deletions are reconciled only where the request window is exactly a set of civil days: Fitbit
        # ranges, and live-verified Google roll-ups / civil- or date-filtered types.
        reconcile = row["category"] != "snapshot" and not row["resumed_mid_pagination"] and cursor is None \
            and row["window_start"] is not None and (
                spec is None or (spec.verified and (spec.filter_kind in (reg.CIVIL, reg.DATE)
                                                    or spec.operation == reg.DAILY_ROLLUP)))
        if cursor is not None and not row["resumed_mid_pagination"]:
            self._update_request(ctx, key, resumed_mid_pagination=1)
        seen: dict[str, set] = {}
        seen_page_tokens: set[str] = set()
        pages, n_records = row["pages"], row["records"]
        while True:
            ctx.checkpoint()
            path = row["path"]
            try:
                if provider == "google" and params.get("op") == "rollup":
                    query = reg.rollup_body(date.fromisoformat(row["window_start"]),
                                            date.fromisoformat(row["window_end"]), cursor)
                    payload = client.rollup(path, query)
                elif provider == "google" and params.get("op") == "list":
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(ctx.params.get("timezone") or "UTC")
                    window = (date.fromisoformat(row["window_start"]), date.fromisoformat(row["window_end"])) \
                        if row["window_start"] else (None, None)
                    payload = self._list_page(ctx, client, spec or reg.SPECS[grp], window[0], window[1], tz, cursor)
                    query = dict(self.last_query)   # actual filter sent, kept with the raw response
                elif provider == "google":           # account snapshots (profile, settings, devices)
                    query = dict(params)
                    if cursor:
                        query["pageToken"] = cursor
                    payload = client.get(path, params=query or None)
                else:
                    query = dict(params)
                    if cursor:
                        path, query = checked_url(catalog.FITBIT_API, cursor), {}
                    payload = client.get(path, params=query or None)
            except ApiError as error:
                if provider == "google" and error.status == 400 and cursor:
                    ctx.warn("{}: page token expired; restarted this chunk from its first page".format(grp))
                    cursor, reconcile = None, False
                    continue
                reason = error.reason
                if provider == "google" and error.status == 400 and spec is not None and not spec.verified:
                    reason += " (this type's filter field is not verified against the live API)"
                status = "denied" if error.status in (401, 403) else "error"
                self._update_request(ctx, key, status="error", http_status=error.status, reason=reason[:300])
                if row["category"] != "snapshot":
                    self.store.set_category_status(provider, grp, row["category"], status,
                                                   "HTTP {}: {}".format(error.status, reason), ctx.id)
                return
            pages += 1
            n_records += self._ingest_page(ctx, provider, grp, row, path, query, payload, totals, seen)
            ctx.progress(current_pages=pages, current_records=n_records, records=totals)
            cursor = self._next_cursor(provider, payload)
            if cursor and provider == "google":
                if cursor in seen_page_tokens:
                    ctx.warn("{}: repeated page token; stopped this chunk".format(grp))
                    cursor, reconcile = None, False
                seen_page_tokens.add(cursor or "")
            if pages - row["pages"] >= MAX_PAGES_PER_REQUEST:
                ctx.warn("{}: reached the {}-page ceiling for one chunk".format(grp, MAX_PAGES_PER_REQUEST))
                cursor, reconcile = None, False
            self._update_request(ctx, key, cursor=cursor, pages=pages, records=n_records)
            if not cursor:
                break
        if reconcile:
            scopes = self._scopes_for(provider, grp, row)
            marked = self.store.reconcile_scopes(provider, {s: seen.get(s, set()) for s in scopes})
            totals["marked_deleted_upstream"] = totals.get("marked_deleted_upstream", 0) + marked
        self._update_request(ctx, key, status="ok", cursor=None)
        if row["category"] != "snapshot":
            self.store.set_category_status(provider, grp, row["category"], "ok" if n_records else "empty",
                                           None if n_records else "Request succeeded with no records", ctx.id)

    def _ingest_page(self, ctx: JobContext, provider: str, grp: str, row: dict, path: str, query: Any,
                     payload: Any, totals: dict, seen: dict) -> int:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw_id = self.store.save_raw(provider, "api", grp, path, query, content, ctx.id,
                                     locale=catalog.FITBIT_LOCALE if provider == "fitbit" else None)
        if row["category"] == "snapshot":
            return 0
        result = self._parse(provider, grp, payload, row)
        spec = reg.SPECS.get(grp) if provider == "google" else None
        if spec is not None and spec.filter_kind == reg.ECG_LOWER_BOUND and row["window_end"]:
            # the API only supports a lower bound for ECG; apply the upper bound here
            result.records = [r for r in result.records if not r.local_date or r.local_date <= row["window_end"]]
        with self.store.transaction() as conn:
            counts = self.store.upsert(provider, self._parser_name(provider), result.records, raw_id, conn)
            for hint, reason in result.unsupported:
                self.store.note_unsupported(provider, "{} {}".format(grp, path)[:300], hint, reason, raw_id, conn)
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        for rec in result.records:
            if rec.scope_key:
                seen.setdefault(rec.scope_key, set()).add((rec.category, rec.metric, rec.record_key))
        for warning in result.warnings:
            ctx.warn(warning)
        return len(result.records)

    def _exercise_windows(self, start: str | None, end: str, pad_s: int) -> list[tuple[str, str]]:
        """Merged, padded UTC windows around stored exercise sessions (health-coach-app's approach)."""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT start_utc, end_utc FROM records WHERE provider='google' AND category='exercise' "
                "AND status='active' AND start_utc IS NOT NULL AND end_utc IS NOT NULL AND local_date <= ? "
                "AND (? IS NULL OR local_date >= ?) ORDER BY start_utc", (end, start, start)).fetchall()
        spans = []
        for r in rows:
            lo, hi = parse_rfc3339(r["start_utc"]), parse_rfc3339(r["end_utc"])
            if lo and hi:
                spans.append((lo - timedelta(seconds=pad_s), hi + timedelta(seconds=pad_s)))
        merged: list[list] = []
        for lo, hi in sorted(spans):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return [(lo.strftime(fmt), hi.strftime(fmt)) for lo, hi in merged]

    def _execute_exercise_windows(self, ctx: JobContext, client: ApiClient, row: dict, params: dict,
                                  totals: dict) -> None:
        grp, key = row["grp"], row["key"]
        spec = reg.SPECS[grp]
        windows = self._exercise_windows(row["window_start"], row["window_end"], spec.window_pad_s)
        index, _, token = (row["cursor"] or "0:").partition(":")
        index, token = int(index), token or None
        pages, n_records = row["pages"], row["records"]
        while index < len(windows):
            ctx.checkpoint()
            lo, hi = windows[index]
            query = {"filter": reg.build_instant_filter(spec, lo, hi), "pageSize": params.get("pageSize", 10000)}
            if token:
                query["pageToken"] = token
            try:
                payload = client.get(row["path"], params=query)
            except ApiError as error:
                status = "denied" if error.status in (401, 403) else "error"
                self._update_request(ctx, key, status="error", http_status=error.status, reason=error.reason[:300])
                self.store.set_category_status("google", grp, row["category"], status,
                                               "HTTP {}: {}".format(error.status, error.reason), ctx.id)
                return
            pages += 1
            n_records += self._ingest_page(ctx, "google", grp, row, row["path"], query, payload, totals, {})
            token = payload.get("nextPageToken") if isinstance(payload, dict) else None
            if not token:
                index += 1
            self._update_request(ctx, key, cursor="{}:{}".format(index, token or ""), pages=pages, records=n_records)
        self._update_request(ctx, key, status="ok", cursor=None)
        message = None if n_records else ("No exercise sessions in range, so no heart-rate windows were fetched"
                                          if not windows else "Request succeeded with no records")
        self.store.set_category_status("google", grp, row["category"], "ok" if n_records else "empty", message,
                                       ctx.id)

    def _scopes_for(self, provider: str, grp: str, row: dict) -> list[str]:
        start, end = date.fromisoformat(row["window_start"]), date.fromisoformat(row["window_end"])
        return ["{}:{}:{}".format(provider, grp, d.isoformat()) for d in daterange(start, end)]

    @staticmethod
    def _parser_name(provider: str) -> str:
        return fitbit_parser.PARSER if provider == "fitbit" else google_parser.PARSER

    @staticmethod
    def _parse(provider: str, grp: str, payload: Any, row: dict) -> ParseResult:
        if provider == "fitbit":
            return fitbit_parser.parse(grp, payload, {"date": row["window_start"]})
        return google_parser.parse_page(grp, payload)

    @staticmethod
    def _next_cursor(provider: str, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        if provider == "google":
            return payload.get("nextPageToken") or None
        nxt = (payload.get("pagination") or {}).get("next")
        return checked_url(catalog.FITBIT_API, nxt) if nxt else None

    # ------------------------------------------------------------------ takeout
    def run_takeout(self, ctx: JobContext) -> dict:
        source = Path(ctx.params["path"]).expanduser()
        dest = self.settings.takeout_dir / ctx.id
        marker = dest / ".extraction-complete"
        limits = Limits(self.settings.limit("takeout_max_total_bytes"), self.settings.limit("takeout_max_file_bytes"),
                        self.settings.limit("takeout_max_files"), self.settings.limit("takeout_max_ratio"))
        if not marker.exists():
            if dest.exists():
                shutil.rmtree(dest)
            ctx.message("Extracting with safety limits")
            try:
                extractor = extract(source, dest, limits, ctx.checkpoint)
            except TakeoutError as error:
                raise AuthError("Takeout import stopped: {}".format(error)) from None
            for member, reason in extractor.rejected:
                self.store.note_unsupported("takeout", member, "archive", reason, None)
                ctx.warn("Not extracted: {} ({})".format(member, reason))
            marker.write_text(json.dumps({"files": extractor.files, "bytes": extractor.total,
                                          "rejected": len(extractor.rejected)}))
        done = int(ctx._progress.get("files_processed", 0))
        totals = dict(ctx._progress.get("records") or {})
        providers = set(ctx._progress.get("providers_detected") or [])
        unsupported = int(ctx._progress.get("files_unsupported", 0))
        files = [p for p in iter_files(dest) if p.name != marker.name]
        for index, path in enumerate(files):
            if index < done:
                continue
            ctx.checkpoint()
            rel = path.relative_to(dest).as_posix()
            tag, result = parse_file(path, rel)
            raw_id = self.store.register_raw_path(tag or "takeout", "takeout", "takeout", rel, path, ctx.id)
            with self.store.transaction() as conn:
                if tag and result.records:
                    counts = self.store.upsert(tag, "takeout:" + tag, result.records, raw_id, conn)
                    for k, v in counts.items():
                        totals[k] = totals.get(k, 0) + v
                    providers.add(tag)
                for hint, reason in result.unsupported:
                    self.store.note_unsupported(tag or "takeout", rel, hint, reason, raw_id, conn)
            if tag is None:
                unsupported += 1
            for warning in result.warnings[:3]:
                ctx.warn(warning)
            ctx.progress(files_total=len(files), files_processed=index + 1, files_unsupported=unsupported,
                         records=totals, providers_detected=sorted(providers), extracted_to=str(dest))
        ctx.progress(force=True, files_total=len(files), files_processed=len(files), records=totals,
                     providers_detected=sorted(providers), files_unsupported=unsupported)
        if not providers:
            return {"status": "partial", "message": "No supported health data files were recognized. The extracted "
                                                    "files are kept; see list_data_types for unsupported formats."}
        return {"status": "succeeded", "message": "Takeout imported. {} file(s) were not in a supported format and "
                                                  "are listed by list_data_types.".format(unsupported)}
