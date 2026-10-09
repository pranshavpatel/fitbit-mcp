"""HTTP client behaviour and category-aware parsing (SYNTHETIC responses)."""
from __future__ import annotations

import pytest
from conftest import seed_token
from fakes import FakeResponse, FakeTokenServer, g_sleep, g_steps, g_weight

from fitbit_mcp.client import ApiClient, ApiError, RateLimitExhausted, checked_url, retry_delay
from fitbit_mcp.oauth import OAuthSession
from fitbit_mcp.parsers import fitbit as fp
from fitbit_mcp.parsers import google as gp


@pytest.fixture
def api(settings, http):
    server = FakeTokenServer(http, "fitbit")
    seed_token(settings, "fitbit", "activity", server=server)
    waits = []
    client = ApiClient(OAuthSession(settings, "fitbit", http=http), waiter=lambda s, r: waits.append((s, r)),
                       http=http)
    return client, waits


def test_get_only_and_provider_host_only():
    assert checked_url("https://api.fitbit.com", "/1/x.json") == "https://api.fitbit.com/1/x.json"
    for url in ["https://evil.test/x", "https://api.fitbit.com.evil.test/x", "https://api.fitbit.com@evil.test/x",
                "http://api.fitbit.com/x"]:
        with pytest.raises(ApiError):
            checked_url("https://api.fitbit.com", url)
    assert not hasattr(ApiClient, "post") and not hasattr(ApiClient, "delete")


def test_401_refreshes_once_then_retries(api, http):
    client, _ = api
    responses = iter([FakeResponse(401), FakeResponse(200, {"ok": True})])
    http.on_get("x.json", lambda *a: next(responses))
    assert client.get("/1/x.json") == {"ok": True}
    assert len([c for c in http.calls if c[0] == "POST"]) == 1
    assert http.gets("x.json")[1][3]["Authorization"] == "Bearer synthetic-access-1"


def test_rate_limit_waits_via_cancellable_waiter(api, http):
    client, waits = api
    responses = iter([FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(200, {"ok": 1})])
    http.on_get("y.json", lambda *a: next(responses))
    import fitbit_mcp.client as cm
    original = cm.time.time
    clock = [original()]
    cm.time.time = lambda: clock[0]

    def waiter(seconds, reason):
        waits.append((seconds, reason))
        clock[0] += seconds
    client.waiter = waiter
    try:
        assert client.get("/1/y.json") == {"ok": 1}
    finally:
        cm.time.time = original
    assert waits and waits[0][1] == "provider rate limit" and 1 <= waits[0][0] <= 3


def test_rate_limit_exhausted_when_waiting_disabled(api, http):
    client, _ = api
    client.wait = False
    http.on_get("z.json", lambda *a: FakeResponse(429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimitExhausted):
        client.get("/1/z.json")


def test_server_errors_retry_but_403_does_not(api, http):
    client, waits = api
    responses = iter([FakeResponse(503), FakeResponse(200, {"ok": 2})])
    http.on_get("a.json", lambda *a: next(responses))
    assert client.get("/1/a.json") == {"ok": 2}
    http.on_get("b.json", lambda *a: FakeResponse(403, {"errors": [{"errorType": "insufficient_scope"}]}))
    with pytest.raises(ApiError, match="insufficient_scope"):
        client.get("/1/b.json")
    assert len(http.gets("b.json")) == 1


def test_retry_delay_parsing():
    assert retry_delay({"Retry-After": "25"}, 60) == 25
    assert retry_delay({"fitbit-rate-limit-reset": "12"}, 60) == 13
    assert retry_delay({"Retry-After": "nonsense"}, 60) == 61


# ------------------------------------------------------------------- Fitbit parsers

def test_fitbit_steps_zero_is_flagged_not_trusted():
    result = fp.parse("activity-steps", {"activities-steps": [{"dateTime": "2026-10-01", "value": "0"},
                                                              {"dateTime": "2026-10-02", "value": "8123"}]})
    first, second = result.records
    assert first.value == 0 and "zero_may_mean_no_data" in first.quality
    assert second.value == 8123 and second.unit == "count" and second.time_basis == "date_only"
    assert second.scope_key == "fitbit:activity-steps:2026-10-02"


def test_fitbit_missing_resting_hr_is_absent_not_zero():
    result = fp.parse("heart", {"activities-heart": [
        {"dateTime": "2026-10-01", "value": {"heartRateZones": []}},
        {"dateTime": "2026-10-02", "value": {"restingHeartRate": 57, "heartRateZones": []}}]})
    rhr = [r for r in result.records if r.metric == "resting_heart_rate"]
    assert [(r.local_date, r.value) for r in rhr] == [("2026-10-02", 57.0)]


def test_fitbit_sleep_session_metrics_and_wake_date():
    log = {"logId": 99, "dateOfSleep": "2026-10-02", "startTime": "2026-10-01T23:30:00.000",
           "endTime": "2026-10-02T07:00:00.000", "minutesAsleep": 410, "minutesAwake": 40, "timeInBed": 450,
           "efficiency": 91, "isMainSleep": True, "levels": {"summary": {"deep": {"minutes": 70, "count": 4}}}}
    result = fp.parse("sleep", {"sleep": [log]})
    metrics = {r.metric: r for r in result.records}
    assert metrics["minutes_asleep"].value == 410 and metrics["minutes_asleep"].local_date == "2026-10-02"
    assert metrics["stage_deep_minutes"].value == 70
    assert metrics["efficiency"].unit == "%"
    assert {r.record_key for r in result.records} == {"99"}


def test_fitbit_weight_units_and_no_fat_double_count():
    result = fp.parse("weight", {"weight": [{"logId": 5, "date": "2026-10-02", "time": "07:00:00", "weight": 160.0,
                                             "bmi": 22.1, "fat": 18.0, "source": "Aria"}]})
    assert {(r.metric, r.unit) for r in result.records} == {("weight", "lb"), ("bmi", "kg/m2")}


def test_fitbit_unknown_group_is_reported_not_dropped():
    result = fp.parse("ecg", {"ecgReadings": []})
    assert result.unsupported and not result.records


def test_fitbit_workout_preserves_offset_and_utc():
    result = fp.parse("workouts", {"activities": [{"logId": 7, "activityName": "Run",
                                                   "startTime": "2026-10-02T07:00:00.000-04:00", "duration": 1800000,
                                                   "calories": 300, "distance": 3.1, "distanceUnit": "Mile"}]})
    duration = [r for r in result.records if r.metric == "duration"][0]
    assert duration.start_utc == "2026-10-02T11:00:00Z" and duration.utc_offset_seconds == -14400
    assert duration.value == 30 and [r.unit for r in result.records if r.metric == "distance"] == ["mi"]


# ------------------------------------------------------------------- Google parsers

def test_google_steps_interval_preserves_offset_and_source():
    rec = gp.parse_page("steps", {"dataPoints": [g_steps("a1", "2026-10-02", 512)]}).records[0]
    assert rec.value == 512 and rec.unit == "count" and rec.local_date == "2026-10-02"
    assert rec.start_utc == "2026-10-02T14:00:00Z" and rec.utc_offset_seconds == -14400
    assert rec.data_source["device"]["displayName"] == "tracker"
    assert rec.record_key == "a1" and rec.time_basis == "utc_with_offset"   # resource-name id, as in health-coach-app


def test_google_sleep_attributed_to_wake_date():
    records = gp.parse_page("sleep", {"dataPoints": [g_sleep("s1", "2026-10-02", 420, prev_day="2026-10-01")]}).records
    asleep = [r for r in records if r.metric == "minutes_asleep"][0]
    assert asleep.local_date == "2026-10-02" and asleep.value == 420
    assert asleep.scope_key == "google:sleep:2026-10-02"


def test_google_weight_grams_kept_in_original_unit():
    rec = gp.parse_page("weight", {"dataPoints": [g_weight("w1", "2026-10-02", 72500.0)]}).records[0]
    assert (rec.metric, rec.value, rec.unit) == ("weight", 72500.0, "g")


def test_google_generic_type_infers_units_from_documented_suffixes():
    point = {"name": "users/me/dataTypes/future-type/dataPoints/d1", "dataSource": {},   # type unknown to us
             "futureType": {"interval": {"startTime": "2026-10-02T14:00:00Z", "startUtcOffset": "-14400s",
                                       "endTime": "2026-10-02T14:10:00Z", "endUtcOffset": "-14400s"},
                          "millimeters": "1500000", "mystery": 3}}
    records = {r.metric: r for r in gp.parse_page("future-type", {"dataPoints": [point]}).records}
    assert records["millimeters"].unit == "mm"
    assert records["mystery"].unit == "unknown" and "unit_unknown" in records["mystery"].quality
    # civil date derived from the UTC offset when civil fields are absent
    assert records["millimeters"].local_date == "2026-10-02" and records["millimeters"].start_local == \
        "2026-10-02T10:00:00"


def test_google_enum_only_records_are_preserved():
    point = {"name": "users/me/dataTypes/moods/dataPoints/m1", "dataSource": {},
             "moods": {"sampleTime": {"physicalTime": "2026-10-02T14:00:00Z", "utcOffset": "-14400s"},
                       "moods": ["HAPPY", "CONTENT"], "valences": ["PLEASANT"]}}
    rec = gp.parse_page("moods", {"dataPoints": [point]}).records[0]
    assert rec.metric == "entry" and rec.value is None
    assert rec.value_text == "moods=HAPPY,CONTENT; valences=PLEASANT" and rec.category == "symptoms_and_mood"


def test_google_unrecognized_point_reported():
    result = gp.parse_page("steps", {"dataPoints": [{"name": "x", "weird": 1}]})
    assert result.unsupported and not result.records


def test_date_chunks_are_contiguous_inclusive_and_handle_leap_days():
    from datetime import date, timedelta
    from fitbit_mcp.util import chunks, daterange, parse_date
    parts = list(chunks(date(2024, 1, 1), date(2024, 3, 31), 31))
    assert parts[0] == (date(2024, 3, 1), date(2024, 3, 31))          # newest first
    covered = sorted(d for s, e in parts for d in daterange(s, e))
    assert covered == list(daterange(date(2024, 1, 1), date(2024, 3, 31)))   # no gaps, no overlaps
    assert date(2024, 2, 29) in covered and all((e - s).days < 31 for s, e in parts)
    assert list(chunks(date(2026, 10, 7), date(2026, 10, 7), 30)) == [(date(2026, 10, 7), date(2026, 10, 7))]
    with pytest.raises(ValueError):
        parse_date("2025-02-29")
    with pytest.raises(ValueError):
        parse_date("2026-1-01")
    assert parse_date("2024-02-29") + timedelta(days=1) == date(2024, 3, 1)


def test_registry_filters_match_health_coach_app_formats():
    from datetime import date
    from zoneinfo import ZoneInfo
    from fitbit_mcp import google_registry as reg
    tz = ZoneInfo("America/New_York")
    assert reg.build_filter(reg.SPECS["daily-heart-rate-variability"], date(2026, 8, 1), date(2026, 8, 14), tz) == \
        'daily_heart_rate_variability.date >= "2026-08-01" AND daily_heart_rate_variability.date < "2026-08-15"'
    assert reg.build_filter(reg.SPECS["sleep"], date(2026, 8, 1), date(2026, 8, 1), tz) == \
        'sleep.interval.civil_end_time >= "2026-08-01T00:00:00" AND sleep.interval.civil_end_time < "2026-08-02T00:00:00"'
    # physical filters: local midnight converted to UTC, across a DST change (EDT -> EST on 2026-11-01)
    assert reg.build_filter(reg.SPECS["heart-rate"], date(2026, 10, 31), date(2026, 11, 1), tz) == \
        ('heart_rate.sample_time.physical_time >= "2026-10-31T04:00:00Z" AND '
         'heart_rate.sample_time.physical_time < "2026-11-02T05:00:00Z"')
    body = reg.rollup_body(date(2026, 9, 1), date(2026, 9, 14), "tok")
    assert body["range"]["end"]["date"] == {"year": 2026, "month": 9, "day": 15} and body["windowSizeDays"] == 1
    assert body["pageToken"] == "tok"
    assert all(s.max_chunk_days <= 14 for s in reg.SPECS_LIST if s.operation == reg.DAILY_ROLLUP)
    assert reg.SPECS["sleep"].page_size == 25 and reg.SPECS["exercise"].page_size == 25
    assert len(reg.VERIFIED) == 23


def test_rollup_rows_daily_keys_and_nan_guard():
    from fakes import civil
    page = {"rollupDataPoints": [{"civilStartTime": civil("2026-09-01"), "civilEndTime": civil("2026-09-02"),
                                  "steps": {"countSum": "8123"}}]}
    rec = gp.parse_page("steps", page).records[0]
    assert (rec.record_key, rec.local_date, rec.value, rec.granularity) == ("2026-09-01", "2026-09-01", 8123, "daily")
    minutes = gp.parse_page("active-minutes", {"rollupDataPoints": [{
        "civilStartTime": civil("2026-09-01"), "activeMinutes": {"activeMinutesRollupByActivityLevel": [
            {"activityLevel": "MODERATE", "activeMinutesSum": "22"}]}}]}).records
    assert [(r.metric, r.value) for r in minutes] == [("active_minutes_moderate", 22.0)]
    daily = {"dataSource": {"platform": "FITBIT", "device": {"displayName": "Air"}, "recordingMethod": "DERIVED"},
             "dailySleepTemperatureDerivations": {"date": {"year": 2026, "month": 9, "day": 1},
                                                  "nightlyTemperatureCelsius": 33.6,
                                                  "baselineTemperatureCelsius": "NaN"}}
    records = gp.parse_page("daily-sleep-temperature-derivations", {"dataPoints": [daily]}).records
    assert [(r.metric, r.value) for r in records] == [("skin_temperature_nightly", 33.6)]   # "NaN" is missing
    assert records[0].record_key == "2026-09-01|FITBIT/Air/DERIVED"
