"""SYNTHETIC test doubles for the Fitbit and Google Health APIs.

All data here is invented for tests. No real account, token or health record is used.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, content=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}
        self.content = content if content is not None else json.dumps(self._payload).encode()

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHTTP:
    """Routes requests to handler callables; records every call (without asserting on tokens)."""

    def __init__(self):
        self.get_handlers = []
        self.post_handlers = []
        self.calls = []
        self.lock = threading.Lock()

    def on_get(self, pattern, handler):
        self.get_handlers.append((re.compile(pattern), handler))

    def on_post(self, pattern, handler):
        self.post_handlers.append((re.compile(pattern), handler))

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=None):
        with self.lock:
            self.calls.append(("GET", url, dict(params or {}), dict(headers or {})))
        for pattern, handler in self.get_handlers:
            if pattern.search(url):
                return handler(url, dict(params or {}), dict(headers or {}))
        return FakeResponse(404, {"errors": [{"errorType": "not_found"}]})

    def post(self, url, data=None, auth=None, timeout=None, allow_redirects=None, json=None, headers=None):
        body = json if json is not None else dict(data or {})
        with self.lock:
            self.calls.append(("POST", url, body, auth if json is None else dict(headers or {})))
        for pattern, handler in self.post_handlers:
            if pattern.search(url):
                return handler(url, body, auth)
        return FakeResponse(404, {"error": "not_found"})

    def posts(self, fragment=""):
        return [c for c in self.calls if c[0] == "POST" and fragment in c[1]]

    def gets(self, fragment=""):
        return [c for c in self.calls if c[0] == "GET" and fragment in c[1]]


class FakeTokenServer:
    """Synthetic OAuth token endpoint that rotates single-use refresh tokens like Fitbit."""

    def __init__(self, http: FakeHTTP, provider="fitbit", scope="activity heartrate sleep weight profile"):
        self.counter = 0
        self.valid_refresh = set()
        self.scope = scope
        self.provider = provider
        self.lock = threading.Lock()
        url = "api.fitbit.com/oauth2/token" if provider == "fitbit" else "oauth2.googleapis.com/token"
        http.on_post(url, self.handle)

    def issue(self):
        self.counter += 1
        refresh = "synthetic-refresh-{}".format(self.counter)
        self.valid_refresh.add(refresh)
        return {"access_token": "synthetic-access-{}".format(self.counter), "refresh_token": refresh,
                "expires_in": 28800, "scope": self.scope, "user_id": "SYNTH01", "token_type": "Bearer"}

    def handle(self, url, data, auth):
        with self.lock:
            if data.get("grant_type") == "authorization_code":
                if data.get("code") != "synthetic-code" or not data.get("code_verifier"):
                    return FakeResponse(400, {"errors": [{"errorType": "invalid_grant"}]})
                return FakeResponse(200, self.issue())
            if data.get("grant_type") == "refresh_token":
                token = data.get("refresh_token")
                if token not in self.valid_refresh:
                    return FakeResponse(400, {"errors": [{"errorType": "invalid_grant"}]})
                if self.provider == "fitbit":
                    self.valid_refresh.discard(token)  # single use
                issued = self.issue()
                if self.provider == "google":
                    issued.pop("refresh_token")
                return FakeResponse(200, issued)
        return FakeResponse(400, {"error": "unsupported_grant_type"})


def _days(start: str, end: str):
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    while s <= e:
        yield s.isoformat()
        s += timedelta(days=1)


class FakeFitbit:
    """SYNTHETIC legacy Fitbit API. `data` maps date -> dict of daily values."""

    def __init__(self, http: FakeHTTP, data: dict, denied=(), workouts=None, sleep=None, weights=None):
        self.data, self.denied, self.workouts = data, set(denied), workouts or []
        self.sleep, self.weights = sleep or {}, weights or {}
        self.rate_limit_once = set()
        self.fail_paths = set()
        http.on_get(r"api\.fitbit\.com", self.handle)

    def handle(self, url, params, headers):
        assert headers.get("Authorization", "").startswith("Bearer ")
        path = urlsplit(url).path
        for fragment in list(self.rate_limit_once):
            if fragment in path:
                self.rate_limit_once.discard(fragment)
                return FakeResponse(429, {"errors": [{"errorType": "rate_limit"}]},
                                    headers={"Retry-After": "1"})
        for fragment in self.fail_paths:
            if fragment in path:
                return FakeResponse(503, {})
        for fragment in self.denied:
            if fragment in path:
                return FakeResponse(403, {"errors": [{"errorType": "insufficient_permissions"}]})
        if path == "/1/user/-/profile.json":
            return FakeResponse(200, {"user": {"memberSince": "2026-09-20", "displayName": "Synthetic"}})
        if path.endswith("/activities/list.json"):
            return self._workouts(url, params)
        m = re.match(r"^/1/user/-/activities/(\w+)/date/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})\.json$", path)
        if m:
            resource, s, e = m.groups()
            if resource == "heart":
                items = []
                for d in _days(s, e):
                    if d in self.data:
                        value = {"heartRateZones": [{"name": "Fat Burn", "min": 100, "max": 140, "minutes": 10}]}
                        if self.data[d].get("rhr") is not None:
                            value["restingHeartRate"] = self.data[d]["rhr"]
                        items.append({"dateTime": d, "value": value})
                return FakeResponse(200, {"activities-heart": items})
            items = [{"dateTime": d, "value": str(self.data.get(d, {}).get(resource, 0))} for d in _days(s, e)]
            return FakeResponse(200, {"activities-" + resource: items})
        m = re.match(r"^/1\.2/user/-/sleep/date/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})\.json$", path)
        if m:
            logs = [log for d in _days(*m.groups()) for log in self.sleep.get(d, [])]
            return FakeResponse(200, {"sleep": logs})
        m = re.match(r"^/1/user/-/body/log/weight/date/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})\.json$", path)
        if m:
            return FakeResponse(200, {"weight": [w for d in _days(*m.groups()) for w in self.weights.get(d, [])]})
        m = re.search(r"/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})\.json$", path)
        if m:  # other ranged endpoints: empty but valid
            key = {"/hrv/": "hrv", "/br/": "br", "/temp/skin/": "tempSkin", "/cardioscore/": "cardioScore",
                   "/water/": "foods-log-water", "/caloriesIn/": "foods-log-caloriesIn",
                   "active-zone-minutes": "activities-active-zone-minutes", "/fat/": "fat"}
            for fragment, name in key.items():
                if fragment in path:
                    return FakeResponse(200, {name: []})
            if "/spo2/" in path:
                return FakeResponse(200, [])
        return FakeResponse(200, {})

    def _workouts(self, url, params):
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        query.update({k: str(v) for k, v in params.items()})
        offset, limit = int(query.get("offset", 0)), int(query.get("limit", 100))
        page = self.workouts[offset:offset + limit]
        nxt = ""
        if offset + limit < len(self.workouts):
            nxt = "https://api.fitbit.com/1/user/-/activities/list.json?offset={}&limit={}&sort=desc" \
                  "&beforeDate={}".format(offset + limit, limit, query.get("beforeDate", ""))
        return FakeResponse(200, {"activities": page, "pagination": {"next": nxt}})


class FakeGoogle:
    """SYNTHETIC Google Health API v4 that behaves like the live API as documented by health-coach-app:
    snake_case filter fields (checked against the registry), civil / physical / date filter values,
    POST dailyRollUp with an exclusive civil end date, page caps, and optional empty first pages."""

    def __init__(self, http: FakeHTTP, points: dict, page_size_cap=2, denied=(), linked=True, rollups=None,
                 empty_first_page=(), accept_fields=None):
        self.points, self.cap, self.denied, self.linked = points, page_size_cap, set(denied), linked
        self.accept_fields = accept_fields or {}   # simulate the live API accepting a different filter form
        self.rollups = rollups or {}          # type -> {day: payload}
        self.empty_first_page = set(empty_first_page)
        self.filters = []
        self.rollup_ranges = []
        http.on_get(r"health\.googleapis\.com", self.handle)
        http.on_post(r"health\.googleapis\.com/v4/users/me/dataTypes/.+:dailyRollUp", self.handle_rollup)

    def handle(self, url, params, headers):
        assert headers.get("Authorization", "").startswith("Bearer ")
        path = urlsplit(url).path
        if path == "/v4/users/me/identity":
            return FakeResponse(200, {"healthUserId": "synthetic-health-user"} if self.linked else {})
        if path in ("/v4/users/me/profile", "/v4/users/me/settings", "/v4/users/me/irnProfile",
                    "/v4/users/me/pairedDevices"):
            return FakeResponse(200, {"synthetic": True})
        m = re.match(r"^/v4/users/me/dataTypes/([a-z0-9\-]+)/dataPoints$", path)
        if not m:
            return FakeResponse(404, {"error": {"status": "NOT_FOUND"}})
        kind = m.group(1)
        if kind in self.denied:
            return FakeResponse(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
        from fitbit_mcp import google_registry as reg
        flt = params.get("filter", "")
        self.filters.append(flt)
        spec = reg.SPECS[kind]
        accepted = self.accept_fields.get(kind, spec.filter_field)
        if spec.filter_kind == reg.NO_FILTER:
            if flt:
                return FakeResponse(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})
            points = list(self.points.get(kind, []))
        elif spec.filter_kind == reg.ECG_LOWER_BOUND:
            match = re.match(r'^electrocardiogram\.interval\.start_time >= "([^"]+)"$', flt)
            if not match:   # only a lower bound on start_time is supported for ECG
                return FakeResponse(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})
            points = [p for p in self.points.get(kind, []) if p["electrocardiogram"]["interval"]["startTime"]
                      >= match.group(1)]
        else:
            match = re.match(r'^([a-z0-9_.]+) >= "([^"]+)" AND ([a-z0-9_.]+) < "([^"]+)"$', flt)
            if not match or match.group(1) != match.group(3) or match.group(1) != accepted:
                return FakeResponse(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})
            lo, hi = match.group(2), match.group(4)
            points = [p for p in self.points.get(kind, []) if lo <= _filter_value(kind, p, lo, accepted) < hi]
        token = params.get("pageToken", "")
        if kind in self.empty_first_page and not token and points:
            return FakeResponse(200, {"dataPoints": [], "nextPageToken": "tok-0"})
        start = int(token.replace("tok-", "") or 0)
        page = points[start:start + self.cap]
        body = {"dataPoints": page}
        if start + self.cap < len(points):
            body["nextPageToken"] = "tok-{}".format(start + self.cap)
        return FakeResponse(200, body)

    def handle_rollup(self, url, body, headers):
        kind = re.search(r"dataTypes/([a-z0-9\-]+)/dataPoints:dailyRollUp", url).group(1)
        if kind in self.denied:
            return FakeResponse(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
        s, e = body["range"]["start"]["date"], body["range"]["end"]["date"]
        lo = "{:04d}-{:02d}-{:02d}".format(s["year"], s["month"], s["day"])
        hi = "{:04d}-{:02d}-{:02d}".format(e["year"], e["month"], e["day"])
        if (date.fromisoformat(hi) - date.fromisoformat(lo)).days > 14:
            return FakeResponse(400, {"error": {"status": "INVALID_ARGUMENT", "details": [
                {"reason": "INVALID_ROLLUP_QUERY_DURATION"}]}})
        self.rollup_ranges.append((kind, lo, hi))
        rows = []
        for day, payload in sorted(self.rollups.get(kind, {}).items()):
            if lo <= day < hi:     # empty days are omitted, as on the live API
                nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
                rows.append({"civilStartTime": civil(day), "civilEndTime": civil(nxt), _camel(kind): payload})
        return FakeResponse(200, {"rollupDataPoints": rows})


def _camel(kind):
    head, *rest = kind.split("-")
    return head + "".join(w.capitalize() for w in rest)


def _filter_value(kind, point, sample_bound, field=""):
    """Value of the point compared by the filter, formatted like the bound."""
    body = point[_camel(kind)]
    if field.endswith(".sample_time.civil_time"):
        c = body["sampleTime"]["civilTime"]
        d, t = c["date"], c.get("time", {})
        return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:00".format(d["year"], d["month"], d["day"], t.get("hours", 0),
                                                            t.get("minutes", 0))
    if sample_bound.endswith("Z"):
        if "sampleTime" in body:
            return body["sampleTime"]["physicalTime"]
        return body["interval"]["startTime"]
    day = _civil_date(kind, point)
    if "T" in sample_bound:
        return day + "T00:00:00" if "interval" not in body else _civil_dt(kind, point)
    return day


def _civil_dt(kind, point):
    body = point[_camel(kind)]
    civil_value = body["interval"]["civilEndTime" if kind == "sleep" else "civilStartTime"]
    d, t = civil_value["date"], civil_value.get("time", {})
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:00".format(d["year"], d["month"], d["day"], t.get("hours", 0),
                                                        t.get("minutes", 0))


def _civil_date(kind, point):
    body = point[_camel(kind)]
    if "interval" in body:
        civil = body["interval"]["civilEndTime" if kind == "sleep" else "civilStartTime"]["date"]
    elif "sampleTime" in body:
        civil = body["sampleTime"]["civilTime"]["date"]
    else:
        civil = body["date"]
    return "{:04d}-{:02d}-{:02d}".format(civil["year"], civil["month"], civil["day"])


def civil(day: str, hour=0, minute=0):
    y, m, d = map(int, day.split("-"))
    return {"date": {"year": y, "month": m, "day": d}, "time": {"hours": hour, "minutes": minute}}


def g_steps(name, day, count, hour=10, source="tracker"):
    """SYNTHETIC Google steps interval DataPoint (UTC-4 offset)."""
    return {"name": "users/me/dataTypes/steps/dataPoints/" + name,
            "dataSource": {"recordingMethod": "AUTOMATIC", "device": {"displayName": source}},
            "steps": {"interval": {"startTime": "{}T{:02d}:00:00Z".format(day, hour + 4), "startUtcOffset": "-14400s",
                                   "endTime": "{}T{:02d}:15:00Z".format(day, hour + 4), "endUtcOffset": "-14400s",
                                   "civilStartTime": civil(day, hour), "civilEndTime": civil(day, hour, 15)},
                      "count": str(count)}}


def g_sleep(name, wake_day, asleep, start_hour_utc=3, prev_day=None):
    """SYNTHETIC Google sleep session ending on wake_day."""
    return {"name": "users/me/dataTypes/sleep/dataPoints/" + name, "dataSource": {"recordingMethod": "AUTOMATIC"},
            "sleep": {"interval": {"startTime": "{}T{:02d}:00:00Z".format(wake_day, start_hour_utc),
                                   "startUtcOffset": "-14400s",
                                   "endTime": "{}T{:02d}:00:00Z".format(wake_day, start_hour_utc + 8),
                                   "endUtcOffset": "-14400s",
                                   "civilStartTime": civil(prev_day or wake_day, 23),
                                   "civilEndTime": civil(wake_day, 7)},
                      "type": "STAGES",
                      "summary": {"minutesAsleep": str(asleep), "minutesAwake": "30",
                                  "stagesSummary": [{"type": "DEEP", "minutes": "60", "count": "3"}]}}}


def g_weight(name, day, grams):
    return {"name": "users/me/dataTypes/weight/dataPoints/" + name, "dataSource": {"recordingMethod": "MANUAL"},
            "weight": {"sampleTime": {"physicalTime": "{}T11:00:00Z".format(day), "utcOffset": "-14400s",
                                      "civilTime": civil(day, 7)}, "weightGrams": grams}}


def g_daily_rhr(name, day, bpm):
    y, m, d = map(int, day.split("-"))
    # Live daily summaries carry no resource name (health-coach-app keys them by date + source).
    return {"dataSource": {"platform": "FITBIT", "recordingMethod": "DERIVED"},
            "dailyRestingHeartRate": {"date": {"year": y, "month": m, "day": d}, "beatsPerMinute": str(bpm)}}


def g_exercise(name, day, start_hour_utc, minutes=30):
    """SYNTHETIC exercise session (UTC-4)."""
    start = datetime(*map(int, day.split("-")), start_hour_utc, tzinfo=timezone.utc)
    end = start + timedelta(minutes=minutes)
    return {"name": "users/me/dataTypes/exercise/dataPoints/" + name, "dataSource": {"platform": "FITBIT"},
            "exercise": {"interval": {"startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "startUtcOffset": "-14400s",
                                      "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "endUtcOffset": "-14400s",
                                      "civilStartTime": civil(day, start_hour_utc - 4)},
                         "exerciseType": "WALKING", "displayName": "Walk",
                         "metricsSummary": {"caloriesKcal": 120.5, "steps": "3000",
                                            "heartRateZoneDurations": {"moderateTime": "600s"}}}}


def g_hr(instant, bpm):
    return {"name": "users/me/dataTypes/heart-rate/dataPoints/x", "dataSource": {"platform": "FITBIT"},
            "heartRate": {"sampleTime": {"physicalTime": instant, "utcOffset": "-14400s"},
                          "beatsPerMinute": str(bpm)}}


def g_sample(kind, name, instant, civil_day, hour, **fields):
    """SYNTHETIC sample-type DataPoint (moods, symptoms, ovulation-test, body-fat, ...)."""
    key = _camel(kind)
    return {"name": "users/me/dataTypes/{}/dataPoints/{}".format(kind, name), "dataSource": {"platform": "FITBIT"},
            key: dict({"sampleTime": {"physicalTime": instant, "utcOffset": "-14400s",
                                      "civilTime": civil(civil_day, hour)}}, **fields)}
