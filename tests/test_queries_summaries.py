"""Query validation, pagination, export and summary correctness (SYNTHETIC records)."""
from __future__ import annotations

import csv
import json

import pytest

from fitbit_mcp import queries, summaries
from fitbit_mcp.parsers.base import Rec
from fitbit_mcp.store import Store


@pytest.fixture
def store(settings):
    return Store(settings.db_path, settings.home)


def daily(metric, day, value, unit="count", category="activity", quality=None):
    return Rec(category=category, metric=metric, granularity="daily", record_key=day, value=value, unit=unit,
               local_date=day, time_basis="date_only", quality=quality or [])


def interval(metric, day, hour, value, source, key):
    return Rec(category="activity", metric=metric, granularity="interval", record_key=key, value=value, unit="count",
               local_date=day, start_local="{}T{:02d}:00:00".format(day, hour), time_basis="utc_with_offset",
               data_source={"device": source})


def test_query_filters_pagination_and_validation(store):
    store.upsert("fitbit", "test", [daily("steps", "2026-10-{:02d}".format(d), 1000 * d) for d in range(1, 21)], None)
    page = queries.query(store, category="activity", metric="steps", start_date="2026-10-05",
                         end_date="2026-10-14", limit=4)
    seen = [r["local_date"] for r in page["records"]]
    while "next_cursor" in page:
        page = queries.query(store, category="activity", metric="steps", start_date="2026-10-05",
                             end_date="2026-10-14", limit=4, cursor=page["next_cursor"])
        seen += [r["local_date"] for r in page["records"]]
    assert seen == ["2026-10-{:02d}".format(d) for d in range(5, 15)]          # inclusive, no gaps or repeats
    assert queries.query(store, min_value=15000, max_value=16000)["count"] == 2
    desc = queries.query(store, metric="steps", order="desc", limit=1)
    assert desc["records"][0]["local_date"] == "2026-10-20"
    for bad in [dict(category="steps; DROP TABLE records"), dict(start_date="2026-13-01"),
                dict(start_date="2026-10-10", end_date="2026-10-01"), dict(provider="evil"),
                dict(category="nonexistent"), dict(limit=5000), dict(cursor="garbage!!")]:
        with pytest.raises(ValueError):
            queries.query(store, **bad)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 20


def test_export_csv_and_json(store, settings):
    store.upsert("fitbit", "test", [daily("steps", "2026-10-01", 4321)], None)
    out = queries.export(store, settings, fmt="csv", metric="steps")
    rows = list(csv.DictReader(open(out["path"], encoding="utf-8")))
    assert rows[0]["value"] == "4321.0" and rows[0]["unit"] == "count" and out["records"] == 1
    assert oct(__import__("os").stat(out["path"]).st_mode & 0o777) == "0o600"
    out = queries.export(store, settings, fmt="json", metric="steps", include_original=True)
    data = json.load(open(out["path"], encoding="utf-8"))
    assert data["records"][0]["local_date"] == "2026-10-01"
    with pytest.raises(ValueError):
        queries.export(store, settings, fmt="json", metric="nothing_here")


def test_steps_summary_never_double_counts_sources_or_providers(store):
    days = ["2026-10-0{}".format(d) for d in range(1, 8)]
    # Fitbit daily totals on days 1-4, plus intraday minutes that must NOT be added on top
    store.upsert("fitbit", "t", [daily("steps", d, 8000) for d in days[:4]], None)
    store.upsert("fitbit", "t", [interval("steps", days[0], h, 500, "tracker", "i{}".format(h)) for h in range(5)],
                 None)
    # Google copy of the same days (another provider) must not be added
    store.upsert("google", "t", [interval("steps", d, 9, 7000, "tracker", "g" + d) for d in days[:4]], None)
    # Days 5-6: two overlapping sources from the same provider -> only one used
    store.upsert("fitbit", "t", [interval("steps", days[4], 9, 3000, "tracker", "a"),
                                 interval("steps", days[4], 10, 3000, "tracker", "b"),
                                 interval("steps", days[4], 9, 5000, "phone", "c")], None)
    result = summaries.summarize(store, "steps", "2026-10-01", "2026-10-07", "day")
    assert result["provider_used"] == "fitbit"
    assert result["other_providers_with_data"] == [{"provider": "google", "days": 4}]
    by_day = {p["period"]: p for p in result["periods"]}
    assert by_day["2026-10-01"]["mean"] == 8000           # daily total wins over intraday
    assert by_day["2026-10-05"]["mean"] == 6000           # tracker (2 records) chosen, phone excluded
    assert by_day["2026-10-07"]["days_with_data"] == 0    # missing day is missing, not zero
    assert result["overall"]["days_with_data"] == 5 and result["overall"]["coverage_pct"] == 71.4
    assert result["exclusions"]["days_with_multiple_sources_one_used"] == 1


def test_no_wear_zero_days_are_excluded(store):
    store.upsert("fitbit", "t", [daily("steps", "2026-10-01", 0, quality=["zero_may_mean_no_data"]),
                                 daily("sedentary_minutes", "2026-10-01", 1440, unit="min"),
                                 daily("steps", "2026-10-02", 0, quality=["zero_may_mean_no_data"]),
                                 daily("sedentary_minutes", "2026-10-02", 900, unit="min"),
                                 daily("steps", "2026-10-03", 6000)], None)
    result = summaries.summarize(store, "steps", "2026-10-01", "2026-10-03", "day")
    assert result["overall"]["days_with_data"] == 2 and result["overall"]["mean"] == 3000
    assert result["exclusions"]["excluded_no_wear_days"] == 1


def test_sleep_summary_deduplicates_overlapping_sessions(store):
    def session(key, day, start, end, asleep):
        return Rec(category="sleep", metric="minutes_asleep", granularity="session", record_key=key, value=asleep,
                   unit="min", local_date=day, start_local=start, end_local=end)
    store.upsert("google", "t", [session("a", "2026-10-02", "2026-10-01T23:00:00", "2026-10-02T07:00:00", 420),
                                 session("dup", "2026-10-02", "2026-10-01T23:05:00", "2026-10-02T06:55:00", 410),
                                 session("nap", "2026-10-02", "2026-10-02T14:00:00", "2026-10-02T14:40:00", 35)],
                 None)
    result = summaries.summarize(store, "sleep", "2026-10-02", "2026-10-02", "day")
    assert result["overall"]["mean"] == 455 and result["main_sleep_mean_minutes"] == 420
    assert result["exclusions"]["overlapping_sessions_removed"] == 1


def test_weight_converts_known_units_and_excludes_unknown(store):
    def w(key, day, value, unit, clock):
        return Rec(category="body", metric="weight", granularity="sample", record_key=key, value=value, unit=unit,
                   local_date=day, start_local="{}T{}".format(day, clock))
    store.upsert("fitbit_takeout", "t", [w("1", "2026-10-01", 72000, "g", "07:00:00"),
                                         w("2", "2026-10-01", 160.0, "lb", "20:00:00"),
                                         w("3", "2026-10-02", 158.0, "unknown", "07:00:00")], None)
    result = summaries.summarize(store, "weight", "2026-10-01", "2026-10-02", "day")
    assert result["unit"] == "kg"
    assert result["periods"][0]["mean"] == round(160 * 0.45359237, 2)    # last measurement of the day
    assert result["periods"][1]["days_with_data"] == 0
    assert result["exclusions"]["excluded_unknown_unit"] == 1


def test_summary_without_data_says_so(store):
    result = summaries.summarize(store, "hrv", "2026-10-01", "2026-10-07", "week")
    assert result["days_with_data"] == 0 and "No local records" in result["message"]


def test_trend_reports_slope(store):
    store.upsert("fitbit", "t", [daily("resting_heart_rate", "2026-10-{:02d}".format(d), 60 - d * 0.5, unit="bpm",
                                       category="heart") for d in range(1, 15)], None)
    result = summaries.summarize(store, "resting_heart_rate", "2026-10-01", "2026-10-14", "week")
    assert result["trend"]["available"] and result["trend"]["slope_per_week"] == -3.5
