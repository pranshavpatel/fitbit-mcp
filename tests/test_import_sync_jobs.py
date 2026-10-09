"""Imports, sync, de-duplication, permissions, rate limits, cancellation and restart recovery.

All provider data below is SYNTHETIC.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, timedelta

import pytest
from fakes import FakeFitbit, FakeGoogle, FakeResponse, g_daily_rhr, g_exercise, g_hr, g_sleep, g_weight

from fitbit_mcp import queries, server
from fitbit_mcp.server import _connect, _start_import, _sync

START = date(2026, 9, 20)   # synthetic memberSince
TODAY = date(2026, 10, 7)


def synthetic_days():
    data = {}
    d = START
    while d <= TODAY:
        i = (d - START).days
        data[d.isoformat()] = {"steps": 5000 + i * 100, "minutesSedentary": 600, "distance": 2.5,
                               "rhr": 55 + (i % 3) if i % 5 else None}
        d += timedelta(days=1)
    return data


def sleep_log(log_id, day, asleep=400):
    prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return {"logId": log_id, "dateOfSleep": day, "startTime": prev + "T23:00:00.000",
            "endTime": day + "T07:00:00.000", "minutesAsleep": asleep, "minutesAwake": 30, "timeInBed": 480,
            "efficiency": 90, "isMainSleep": True, "type": "stages", "levels": {"summary": {}}}


def workouts(n):
    return [{"logId": 1000 + i, "activityName": "Walk", "startTime": "2026-09-{:02d}T08:00:00.000-04:00".format(
        20 + i % 10), "duration": 600000, "calories": 50, "steps": 900} for i in range(n)]


@pytest.fixture
def fitbit_api(http):
    sleep = {"2026-09-21": [sleep_log(1, "2026-09-21")], "2026-10-04": [sleep_log(2, "2026-10-04")],
             "2026-10-05": [sleep_log(3, "2026-10-05")]}
    return FakeFitbit(http, synthetic_days(), denied={"/hrv/"}, workouts=workouts(150), sleep=sleep,
                      weights={"2026-10-01": [{"logId": 77, "date": "2026-10-01", "time": "07:00:00",
                                               "weight": 170.0, "bmi": 24.0}]})


def run(app, view, timeout=30):
    return app.jobs.wait_for(view["job_id"], timeout=timeout)


def count(app, sql, *args):
    with app.store.connect() as conn:
        return conn.execute(sql, args).fetchone()[0]


def test_fitbit_full_history_import_with_partial_permissions(make_app, fitbit_tokens, fitbit_api, http):
    app = make_app()
    job = run(app, _start_import(app, "fitbit", None, None, None, False))
    assert job["status"] == "partial"           # hrv denied + several permissions not granted
    steps = queries.query(app.store, category="activity", metric="steps", limit=500)
    dates = [r["local_date"] for r in steps["records"]]
    assert dates[0] == "2026-09-20" and dates[-1] == "2026-10-07" and len(dates) == 18   # inclusive boundaries
    assert all(r["unit"] == "count" for r in steps["records"])
    # missing resting HR stays missing (every 5th synthetic day has none)
    rhr = queries.query(app.store, category="heart", metric="resting_heart_rate", limit=500)
    assert rhr["count"] == 18 - 4
    types = queries.data_types(app.store)
    status = {(s["request_group"], s["status"]) for s in types["missing_denied_or_failed"]}
    assert ("hrv", "denied") in status
    assert ("spo2", "scope_not_granted") in status and ("water", "scope_not_granted") in status
    assert not http.gets("/spo2/")             # never requested without permission
    # workout list pagination followed to the second page
    assert count(app, "SELECT COUNT(DISTINCT record_key) FROM records WHERE category='exercise'") == 150
    assert len(http.gets("activities/list.json")) == 2
    # raw responses retained with provenance
    assert count(app, "SELECT COUNT(*) FROM raw_files WHERE provider='fitbit' AND locale='en_US'") > 10
    with app.store.connect() as conn:
        path = conn.execute("SELECT path FROM raw_files WHERE grp='activity-steps' LIMIT 1").fetchone()[0]
    assert (app.settings.home / path).exists()


def test_reimport_deduplicates_and_sync_refreshes_updates_and_deletions(make_app, fitbit_tokens, fitbit_api, http):
    app = make_app()
    run(app, _start_import(app, "fitbit", None, None, ["activity", "sleep"], False))
    total_before = count(app, "SELECT COUNT(*) FROM records")
    run(app, _start_import(app, "fitbit", None, None, ["activity", "sleep"], False))
    assert count(app, "SELECT COUNT(*) FROM records") == total_before       # no duplicates
    # provider-side changes: an edited day, a deleted sleep log inside the window and one outside it
    fitbit_api.data["2026-10-05"]["steps"] = 12345
    fitbit_api.sleep["2026-10-04"] = []
    fitbit_api.sleep["2026-09-21"] = []
    calls_before = len(http.gets("/activities/steps/date/"))
    job = run(app, _sync(app, "fitbit", 7, ["activity", "sleep"], False))
    assert job["status"] == "succeeded"
    assert job["params"]["window_start"] == "2026-09-30" and job["params"]["window_end"] == "2026-10-07"
    assert len(http.gets("/activities/steps/date/")) == calls_before + 1   # deliberately re-requested
    assert "/2026-09-30/2026-10-07.json" in http.gets("/activities/steps/date/")[-1][1]
    with app.store.connect() as conn:
        edited = conn.execute("SELECT value, revision FROM records WHERE metric='steps' AND local_date='2026-10-05' "
                              "AND provider='fitbit'").fetchone()
        inside = conn.execute("SELECT DISTINCT status FROM records WHERE category='sleep' AND record_key='2'"
                              ).fetchone()[0]
        outside = conn.execute("SELECT DISTINCT status FROM records WHERE category='sleep' AND record_key='1'"
                               ).fetchone()[0]
    assert tuple(edited) == (12345.0, 2)
    assert inside == "deleted_upstream"            # flagged, not erased
    assert outside == "active"                     # outside the overlap window: documented limitation
    assert job["progress"]["records"]["marked_deleted_upstream"] >= 1
    assert count(app, "SELECT COUNT(*) FROM records") == total_before
    hidden = queries.query(app.store, category="sleep", metric="minutes_asleep")["count"]
    shown = queries.query(app.store, category="sleep", metric="minutes_asleep", include_deleted=True)["count"]
    assert shown == hidden + 1


def test_rate_limit_wait_then_success(make_app, fitbit_tokens, fitbit_api):
    fitbit_api.rate_limit_once.add("/activities/steps/")
    app = make_app()
    job = run(app, _start_import(app, "fitbit", "2026-10-01", "2026-10-07", ["activity"], False))
    assert job["status"] == "succeeded"
    assert count(app, "SELECT COUNT(*) FROM records WHERE metric='steps'") == 7


def test_cancel_during_rate_limit_wait_and_resume(make_app, fitbit_tokens, fitbit_api, http):
    original = fitbit_api.handle
    state = {"limited": True}

    def limited(url, params, headers):
        if state["limited"] and "/activities/floors/" in url:
            return FakeResponse(429, {}, headers={"Retry-After": "600"})
        return original(url, params, headers)
    http.get_handlers.insert(0, (__import__("re").compile("api.fitbit.com"), limited))
    app = make_app()
    view = _start_import(app, "fitbit", "2026-10-01", "2026-10-07", ["activity"], False)
    app.jobs.wait_for(view["job_id"], statuses=("waiting_rate_limit",), timeout=20)
    assert app.jobs.get(view["job_id"])["wait_until"]
    started = time.monotonic()
    app.jobs.cancel(view["job_id"])
    job = app.jobs.wait_for(view["job_id"], timeout=5)
    assert job["status"] == "cancelled" and time.monotonic() - started < 3
    with app.store.connect() as conn:
        done = conn.execute("SELECT COUNT(*) FROM job_requests WHERE job_id=? AND status='ok'",
                            (view["job_id"],)).fetchone()[0]
    assert done >= 2
    steps_calls = len(http.gets("/activities/steps/"))
    state["limited"] = False
    from fitbit_mcp import client as cm
    cm._NOT_BEFORE.clear()
    job = app.jobs.wait_for(app.jobs.resume(view["job_id"])["id"], timeout=20)
    assert job["status"] == "succeeded"
    assert len(http.gets("/activities/steps/")) == steps_calls      # completed requests not repeated


class SimulatedCrash(BaseException):
    """Stands in for the server process dying mid-request."""


def test_restart_recovery_resumes_without_refetching(make_app, fitbit_tokens, fitbit_api, http, recwarn):
    original = fitbit_api.handle
    gate = threading.Event()

    def crash_on_floors(url, params, headers):
        if "/activities/floors/" in url and not gate.is_set():
            gate.set()
            raise SimulatedCrash()
        return original(url, params, headers)
    import re
    http.get_handlers.insert(0, (re.compile("api.fitbit.com"), crash_on_floors))
    first = make_app()
    view = _start_import(first, "fitbit", "2026-10-01", "2026-10-07", ["activity"], False)
    assert gate.wait(10)
    time.sleep(1.5)                                                  # > FITBIT_MCP_JOB_STALE_SECONDS in tests
    assert first.jobs.get(view["job_id"])["status"] == "running"     # the "process" died here
    second = make_app()                                              # restart: new server, same database
    job = second.jobs.get(view["job_id"])
    assert job["status"] == "interrupted"
    steps_calls = len(http.gets("/activities/steps/"))
    resumed = second.jobs.wait_for(second.jobs.resume(view["job_id"])["id"], timeout=20)
    assert resumed["status"] == "succeeded"
    assert len(http.gets("/activities/steps/")) == steps_calls
    assert count(second, "SELECT COUNT(*) FROM records WHERE metric='floors'") == 7


# --------------------------------------------------------------------------- Google
# The Google path follows health-coach-app: per-type filters, chunking, roll-ups and
# exercise-scoped heart rate. FakeGoogle rejects any filter field the registry does not name.

def google_points():
    return {
        "sleep": [g_sleep("z1", "2026-10-03", 420, prev_day="2026-10-02"),
                  g_sleep("z2", "2026-10-05", 380, prev_day="2026-10-04")],
        "exercise": [g_exercise("e1", "2026-10-04", 14)],
        "heart-rate": [g_hr("2026-10-04T14:10:00Z", 110), g_hr("2026-10-04T14:20:00Z", 125),
                       g_hr("2026-10-04T20:00:00Z", 70)],       # last one is outside the workout window
        "weight": [g_weight("w1", "2026-10-02", 72000.0)],
        "daily-resting-heart-rate": [g_daily_rhr("r1", "2026-10-02", 54), g_daily_rhr("r2", "2026-10-03", 55)],
    }


def google_rollups():
    return {"steps": {"2026-09-1{}".format(d): {"countSum": str(5000 + d)} for d in range(5, 10)} |
            {"2026-10-0{}".format(d): {"countSum": str(8000 + d)} for d in range(1, 7)},
            "total-calories": {"2026-10-01": {"kcalSum": 2200.5}}}


def test_google_full_history_follows_health_coach_retrieval(make_app, google_tokens, http):
    api = FakeGoogle(http, google_points(), page_size_cap=1, denied={"floors"}, rollups=google_rollups(),
                     empty_first_page={"sleep"})
    app = make_app()
    job = run(app, _start_import(app, "google", None, None, None, False), timeout=60)
    assert job["status"] == "partial"                     # floors denied
    # every list request used the live-verified filter field (FakeGoogle 400s otherwise)
    assert not [r for r in queries.data_types(app.store)["missing_denied_or_failed"] if r["status"] == "error"]
    # steps came from POST dailyRollUp in <= 14-day chunks back to the discovered first day
    origins = job["progress"]["discovered_history_start"]
    assert origins["steps"] == "2026-09-15" and origins["sleep"] == "2026-10-03"
    assert all((date.fromisoformat(hi) - date.fromisoformat(lo)).days <= 14 for _, lo, hi in api.rollup_ranges)
    steps = queries.query(app.store, category="activity", metric="steps", provider="google", limit=500)["records"]
    assert [r["local_date"] for r in steps][:2] == ["2026-09-15", "2026-09-16"] and steps[0]["granularity"] == "daily"
    assert len(steps) == 11
    # sleep's empty first page did not hide data
    assert queries.query(app.store, category="sleep", metric="minutes_asleep")["count"] == 2
    # heart rate was fetched only over the padded exercise window
    hr_filters = sorted({f for f in api.filters if f.startswith("heart_rate.")})   # 2 pages, one window
    assert hr_filters == ['heart_rate.sample_time.physical_time >= "2026-10-04T13:58:00Z" AND '
                          'heart_rate.sample_time.physical_time < "2026-10-04T14:32:00Z"']
    assert queries.query(app.store, metric="heart_rate")["count"] == 2
    # daily summaries without resource names keep stable keys
    rhr = queries.query(app.store, metric="resting_heart_rate")["records"]
    assert [r["value"] for r in rhr] == [54.0, 55.0]
    statuses = {(s["request_group"], s["status"]) for s in queries.data_types(app.store)["missing_denied_or_failed"]}
    assert ("floors", "denied") in statuses


def test_google_sync_restatement_dedup_and_deletions(make_app, google_tokens, http):
    api = FakeGoogle(http, google_points(), page_size_cap=5, rollups=google_rollups())
    app = make_app()
    run(app, _start_import(app, "google", "2026-10-01", "2026-10-06", ["activity", "sleep", "heart"], False))
    total = count(app, "SELECT COUNT(*) FROM records WHERE provider='google'")
    api.rollups["steps"]["2026-10-05"] = {"countSum": "12000"}       # device synced more steps later
    api.points["sleep"] = api.points["sleep"][:1]                       # session z2 (2026-10-05) deleted
    api.points["daily-resting-heart-rate"][1]["dailyRestingHeartRate"]["beatsPerMinute"] = "53"
    job = run(app, _sync(app, "google", 3, ["activity", "sleep", "heart"], False))
    assert job["status"] == "succeeded"
    assert job["params"]["window_start"] == "2026-10-03"               # last sync (10-06) minus 3 days
    with app.store.connect() as conn:
        steps = conn.execute("SELECT value, revision FROM records WHERE provider='google' AND metric='steps' AND "
                             "local_date='2026-10-05'").fetchone()
        z2 = conn.execute("SELECT DISTINCT status FROM records WHERE record_key='z2'").fetchone()[0]
    assert tuple(steps) == (12000.0, 2) and z2 == "deleted_upstream"
    assert queries.query(app.store, metric="resting_heart_rate", start_date="2026-10-03",
                         end_date="2026-10-03")["records"][0]["value"] == 53
    assert count(app, "SELECT COUNT(*) FROM records WHERE provider='google'") == total   # updated, not duplicated


def test_google_filters_match_health_coach_formats(make_app, google_tokens, http, monkeypatch):
    monkeypatch.setenv("FITBIT_MCP_TZ", "America/New_York")
    api = FakeGoogle(http, google_points(), page_size_cap=10, rollups=google_rollups())
    app = make_app()
    job = run(app, _start_import(app, "google", "2026-10-02", "2026-10-03", ["sleep", "body", "heart"], False))
    assert job["status"] == "succeeded"
    assert 'sleep.interval.civil_end_time >= "2026-10-02T00:00:00" AND sleep.interval.civil_end_time < ' \
           '"2026-10-04T00:00:00"' in api.filters
    assert 'weight.sample_time.physical_time >= "2026-10-02T04:00:00Z" AND weight.sample_time.physical_time < ' \
           '"2026-10-04T04:00:00Z"' in api.filters                       # local midnight converted to UTC
    assert 'daily_resting_heart_rate.date >= "2026-10-02" AND daily_resting_heart_rate.date < "2026-10-04"' \
        in api.filters
    assert {r["local_date"] for r in queries.query(app.store, category="sleep")["records"]} == {"2026-10-03"}


def test_google_unlinked_account_is_not_reported_connected(make_app, http, settings):
    from fakes import FakeTokenServer
    FakeTokenServer(http, "google", scope="https://www.googleapis.com/auth/googlehealth.sleep.readonly")
    FakeGoogle(http, {}, linked=False)
    from test_auth import _browser
    app = make_app(open_browser=_browser([]))
    view = _connect(app, "google", ["sleep"], 60)
    job = app.jobs.wait_for(view["job_id"], timeout=20)
    assert job["status"] == "failed" and "not usable" in job["error"]
    status = server._connection_status(app, "google", False)["providers"][0]
    assert status["connected"] is False and status["token_status"] == "active"
    assert "synthetic-access" not in json.dumps(server._job_view(job))


def test_fitbit_connect_end_to_end_reports_success_only_after_verification(make_app, http, fitbit_api):
    from fakes import FakeTokenServer
    from test_auth import _browser
    FakeTokenServer(http, "fitbit", scope="activity profile")
    app = make_app(open_browser=_browser([]))
    view = _connect(app, "fitbit", ["activity", "profile", "sleep"], 60)
    # the worker may already have picked the job up; what matters is it isn't "succeeded" before verification
    assert view["status"] in ("queued", "running")
    job = app.jobs.wait_for(view["job_id"], timeout=20)
    assert job["status"] == "succeeded"
    assert job["progress"]["denied_scopes"] == ["sleep"]
    status = server._connection_status(app, "fitbit", False)["providers"][0]
    assert status["connected"] is True and status["granted_permissions"] == ["activity", "profile"]
    text = json.dumps(status) + json.dumps(server._job_view(job))
    for secret in ("synthetic-access", "synthetic-refresh", "synthetic-code", "SYNTHCLIENT-secret"):
        assert secret not in text


def test_disconnect_requires_confirmation_before_revoking_and_keeps_data(make_app, fitbit_tokens, fitbit_api, http):
    from fitbit_mcp.oauth import TokenStore
    from fitbit_mcp.server import _disconnect
    app = make_app()
    run(app, _start_import(app, "fitbit", "2026-10-01", "2026-10-07", ["activity"], False))
    http.on_post("oauth2/revoke", lambda url, data, auth: FakeResponse(200, {}))
    preview = _disconnect(app, "fitbit", True, False, False, None)
    assert preview["confirmation_required"] and not [c for c in http.calls if "revoke" in c[1]]
    assert TokenStore(app.settings, "fitbit").load()                     # nothing changed yet
    with pytest.raises(Exception):
        _disconnect(app, "fitbit", True, True, False, preview["confirmation_token"])  # different action
    preview = _disconnect(app, "fitbit", True, False, False, None)
    done = _disconnect(app, "fitbit", True, False, False, preview["confirmation_token"])
    assert done["revocation"]["revoked"] is True and done["data_kept"] is True
    assert not TokenStore(app.settings, "fitbit").load()
    assert count(app, "SELECT COUNT(*) FROM records WHERE provider='fitbit'") > 0
    assert server._connection_status(app, "fitbit", False)["providers"][0]["connected"] is False


def test_input_validation_for_imports(make_app, fitbit_tokens):
    from fitbit_mcp.server import ToolError
    app = make_app()
    for args in [("fitbit", "2026-10-05", "2026-10-01", None, False), ("fitbit", None, "2026-12-01", None, False),
                 ("fitbit", None, None, ["reproductive_health"], False), ("google", None, None, None, True)]:
        with pytest.raises(ToolError):
            _start_import(app, *args)
    with pytest.raises(ToolError):
        _connect(app, "fitbit", ["activity", "write_everything"], 60)


def test_connect_reuses_ghealth_cli_credentials_without_browser(make_app, http, settings):
    from fakes import FakeTokenServer
    from test_auth import write_cli_credentials
    from fitbit_mcp import catalog
    server = FakeTokenServer(http, "google", scope=catalog.google_scope("sleep"))
    server.valid_refresh.add("cli-refresh-1")
    write_cli_credentials(settings, "cli-refresh-1")
    FakeGoogle(http, {})
    opened = []
    app = make_app(open_browser=lambda url: opened.append(url) or True)
    view = _connect(app, "google", None, 60)
    assert view["method"] == "ghealth_cli"
    job = app.jobs.wait_for(view["job_id"], timeout=20)
    assert job["status"] == "succeeded" and not opened
    status = server_status = server_mod_status(app)
    assert status["connected"] is True and status["credential_source"] == "ghealth CLI credentials"
    assert "found at" in status["ghealth_cli_credentials"]
    assert "cli-refresh" not in json.dumps(server_status) + json.dumps(job)


def server_mod_status(app):
    return server._connection_status(app, "google", False)["providers"][0]


# ------------------------------------------- all documented Google types (SYNTHETIC data)

def _interval_point(kind, key, name, day, start_utc, end_utc, **fields):
    from fakes import civil
    return {"name": "users/me/dataTypes/{}/dataPoints/{}".format(kind, name), "dataSource": {"platform": "FITBIT"},
            key: dict({"interval": {"startTime": start_utc, "startUtcOffset": "-14400s", "endTime": end_utc,
                                    "endUtcOffset": "-14400s", "civilStartTime": civil(day, 8)}}, **fields)}


def all_type_points():
    from fakes import g_sample
    return {
        "moods": [g_sample("moods", "m1", "2026-10-02T13:00:00Z", "2026-10-02", 9, moods=["HAPPY"],
                           valences=["PLEASANT"])],
        "symptoms": [g_sample("symptoms", "s1", "2026-10-03T13:00:00Z", "2026-10-03", 9, symptoms=["HEADACHE"])],
        "ovulation-test": [g_sample("ovulation-test", "o1", "2026-10-03T12:00:00Z", "2026-10-03", 8,
                                    result="NEGATIVE")],
        "body-fat": [g_sample("body-fat", "b1", "2026-10-02T11:00:00Z", "2026-10-02", 7, percentage=18.5)],
        "menstrual-period": [_interval_point("menstrual-period", "menstrualPeriod", "p1", "2026-10-01",
                                             "2026-10-01T12:00:00Z", "2026-10-05T12:00:00Z")],
        "nutrition-log": [_interval_point("nutrition-log", "nutritionLog", "n1", "2026-10-02", "2026-10-02T12:00:00Z",
                                          "2026-10-02T12:20:00Z", energy={"kcal": 450.0}, foodDisplayName="Oatmeal",
                                          mealType="BREAKFAST")],
        "hydration-log": [_interval_point("hydration-log", "hydrationLog", "h1", "2026-10-02", "2026-10-02T13:00:00Z",
                                          "2026-10-02T13:01:00Z", amountConsumed={"milliliters": 500})],
        "electrocardiogram": [
            _interval_point("electrocardiogram", "electrocardiogram", "ecg1", "2026-10-03", "2026-10-03T14:00:00Z",
                            "2026-10-03T14:00:30Z", beatsPerMinuteAvg="64", resultClassification="NORMAL_SINUS_RHYTHM"),
            _interval_point("electrocardiogram", "electrocardiogram", "ecg2", "2026-10-07", "2026-10-07T14:00:00Z",
                            "2026-10-07T14:00:30Z", beatsPerMinuteAvg="70")],     # after the requested end date
        "food": [{"name": "users/me/dataTypes/food/dataPoints/f1", "food": {"displayName": "Oatmeal",
                                                                            "brand": "Generic"}}],
    }


def test_all_documented_google_types_import_by_default(make_app, http, settings):
    from fakes import FakeTokenServer
    from fitbit_mcp import catalog
    scope = " ".join(catalog.GOOGLE_SCOPES)          # every read-only permission granted
    server = FakeTokenServer(http, "google", scope=scope)
    from conftest import seed_token
    seed_token(settings, "google", scope, server=server)
    # Simulate the live API accepting only the civil-time filter form for moods.
    api = FakeGoogle(http, all_type_points(), page_size_cap=10,
                     accept_fields={"moods": "moods.sample_time.civil_time"})
    app = make_app()
    job = run(app, _start_import(app, "google", "2026-10-01", "2026-10-06", None, False), timeout=60)
    assert job["status"] == "succeeded", queries.data_types(app.store)["missing_denied_or_failed"]
    texts = {r["metric"] + ":" + r.get("value_text", "") for r in
             queries.query(app.store, category="symptoms_and_mood")["records"]}
    assert texts == {"entry:moods=HAPPY; valences=PLEASANT", "entry:HEADACHE"}
    repro = {(r["metric"], r.get("value"), r.get("value_text")) for r in
             queries.query(app.store, category="reproductive_health")["records"]}
    assert ("period_days", 4.0, "menstrual period") in repro and ("entry", None, "NEGATIVE") in repro
    assert queries.query(app.store, metric="body_fat")["records"][0]["value"] == 18.5
    nutrition = {r["metric"]: r for r in queries.query(app.store, category="nutrition")["records"]}
    assert nutrition["calories_in"]["value"] == 450 and nutrition["water"]["unit"] == "mL"
    assert "entry" not in nutrition                     # the shared food catalog is never imported
    ecg = queries.query(app.store, category="ecg")["records"]
    assert {r.get("value_text") or r["value"] for r in ecg} == {64.0, "NORMAL_SINUS_RHYTHM"}   # ecg2 is past the end
    assert 'electrocardiogram.interval.start_time >= "2026-10-01T00:00:00Z"' in api.filters
    assert not http.gets("/dataTypes/food/") and not http.gets("/dataTypes/food-measurement-unit/")
    assert any("alternative filter form 'moods.sample_time.civil_time'" in w for w in job["warnings"])
    assert app.importer.get_meta("filter:google:moods") == 1                  # remembered for later chunks


def test_missing_google_permissions_are_reported_with_instructions(make_app, google_tokens, http):
    FakeGoogle(http, all_type_points(), page_size_cap=10)
    app = make_app()
    job = run(app, _start_import(app, "google", "2026-10-01", "2026-10-06", ["symptoms_and_mood",
                                                                             "reproductive_health"], False))
    assert job["status"] == "partial"
    statuses = {(s["request_group"], s["status"]) for s in queries.data_types(app.store)["missing_denied_or_failed"]}
    assert {("moods", "scope_not_granted"), ("symptoms", "scope_not_granted"),
            ("ovulation-test", "scope_not_granted")} <= statuses
    state = server._connection_status(app, "google", False)["providers"][0]
    assert {"mindfulness", "logged_symptoms", "reproductive_health"} <= set(state["not_granted"])
    assert "Data Access" in state["how_to_grant_missing"]


def test_browser_connect_requests_every_read_permission_except_location(make_app, http, settings):
    from fakes import FakeTokenServer
    from test_auth import _browser, write_cli_credentials
    FakeTokenServer(http, "google", scope="https://www.googleapis.com/auth/googlehealth.sleep.readonly")
    write_cli_credentials(settings, "cli-refresh-1")
    FakeGoogle(http, {})
    seen = []
    app = make_app(open_browser=_browser(seen))
    view = _connect(app, "google", None, 60, "browser")
    app.jobs.wait_for(view["job_id"], timeout=20)
    requested = {s.rsplit(".", 2)[-2] for s in seen[0]["scope"].split()}
    assert {"mindfulness", "logged_symptoms", "reproductive_health", "nutrition", "ecg", "irn"} <= requested
    assert "location" not in requested and all(s.endswith(".readonly") for s in seen[0]["scope"].split())



# ------------------------------------------- several server processes sharing one database

def _rate_limited_floors(http, fitbit_api, retry_after="600"):
    import re
    original = fitbit_api.handle

    def limited(url, params, headers):
        if "/activities/floors/" in url:
            return FakeResponse(429, {}, headers={"Retry-After": retry_after})
        return original(url, params, headers)
    http.get_handlers.insert(0, (re.compile("api.fitbit.com"), limited))


def test_second_server_does_not_disturb_a_live_job_and_can_cancel_it(make_app, fitbit_tokens, fitbit_api, http):
    _rate_limited_floors(http, fitbit_api)
    first = make_app()
    view = _start_import(first, "fitbit", "2026-10-01", "2026-10-07", ["activity"], False)
    first.jobs.wait_for(view["job_id"], statuses=("waiting_rate_limit",), timeout=20)
    time.sleep(1.5)                       # longer than the stale threshold; the live owner keeps heart-beating
    second = make_app()                   # e.g. the desktop app starting while Claude Code's server works
    second.jobs.recover(startup=False)
    assert second.jobs.get(view["job_id"])["status"] == "waiting_rate_limit"
    assert not second.jobs.get(view["job_id"])["stalled"]
    second.jobs.cancel(view["job_id"])    # cancel arrives at the *other* process
    job = first.jobs.wait_for(view["job_id"], timeout=5)
    assert job["status"] == "cancelled"


def test_surviving_server_takes_over_a_job_whose_process_died(make_app, fitbit_tokens, fitbit_api, http,
                                                                monkeypatch):
    import re
    original = fitbit_api.handle
    gate = threading.Event()

    def crash_on_floors(url, params, headers):
        if "/activities/floors/" in url and not gate.is_set():
            gate.set()
            raise SimulatedCrash()
        return original(url, params, headers)
    http.get_handlers.insert(0, (re.compile("api.fitbit.com"), crash_on_floors))
    first = make_app()
    view = _start_import(first, "fitbit", "2026-10-01", "2026-10-07", ["activity"], False)
    assert gate.wait(10)
    monkeypatch.setenv("FITBIT_MCP_AUTO_RESUME", "1")
    second = make_app()                   # already running elsewhere; its heartbeat loop notices the dead owner
    steps_calls = len(http.gets("/activities/steps/"))
    job = second.jobs.wait_for(view["job_id"], statuses=("succeeded",), timeout=20)
    assert job["owner"] == second.jobs.owner
    assert len(http.gets("/activities/steps/")) == steps_calls   # completed requests were not repeated
    assert count(second, "SELECT COUNT(*) FROM records WHERE metric='floors'") == 7



def test_catalog_rows_from_an_older_version_are_hidden_not_deleted(make_app, settings):
    from fitbit_mcp.parsers.base import Rec
    app = make_app()
    app.store.upsert("google", "t", [Rec(category="nutrition", metric="entry", granularity="daily", record_key="60751",
                                         value_text="Chocolates / Catalog Brand",
                                         source_id="users/1/dataTypes/food/dataPoints/60751")], None)
    reopened = make_app()                 # the next server start applies the cleanup
    assert queries.query(reopened.store, provider="google", include_deleted=True)["count"] == 0
    assert not [m for m in queries.data_types(reopened.store)["metrics"] if m["category"] == "nutrition"]
    with reopened.store.connect() as conn:
        assert conn.execute("SELECT status FROM records WHERE record_key='60751'").fetchone()[0] == "excluded_catalog"
