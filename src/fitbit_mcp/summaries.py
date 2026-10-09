"""Descriptive summaries with explicit definitions and coverage.

Rules that keep numbers honest:
* one provider per summary (the one with the most days of data unless you choose), so
  Fitbit, Google and Takeout copies of the same day are never added together;
* within a provider, daily totals are preferred; interval records are summed only for days
  without a daily total, and only from a single data source per day;
* a day without records is missing, never zero; Fitbit zero-step days whose sedentary time
  is a full 1440 minutes (or unknown) are treated as no-wear and excluded;
* values are converted only between known units; unknown units are excluded and counted.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .store import Store
from .util import daterange, parse_date, parse_rfc3339

KM = {"km": 1.0, "mi": 1.609344, "mm": 1e-6, "m": 1e-3}
KG = {"kg": 1.0, "g": 1e-3, "lb": 0.45359237}

DEFINITIONS = {
    "steps": "Daily step total. Uses the provider's daily total when present (Fitbit daily series or Google's "
             "steps daily roll-up); otherwise the sum of that day's "
             "interval step counts from one data source (the source with the most records that day). Days with "
             "no records are missing, not zero. Fitbit days with 0 steps and a full day (or unknown) sedentary "
             "time are treated as no-wear and excluded.",
    "distance": "Daily distance in kilometres, converted from the provider unit (mi, mm, m). Same daily-total "
                "and single-source rules as steps.",
    "calories_out": "Daily total calories burned (kcal, includes BMR): Fitbit's daily summary or Google's "
                    "total-calories daily roll-up. Google active-energy (activity only) is a different definition "
                    "and is not substituted.",
    "sleep": "Minutes asleep per wake date (the date a sleep session ends; Fitbit 'dateOfSleep'). Sessions from "
             "the same day that overlap in time are de-duplicated (longest kept); naps are included in "
             "'total' and the longest session is reported as 'main'.",
    "resting_heart_rate": "Provider-computed daily resting heart rate (bpm). If several sources report a value "
                          "for a day, the dominant source's value is used.",
    "hrv": "Provider-computed daily heart rate variability (ms). Fitbit: dailyRmssd (RMSSD during sleep). "
           "Google: averageHeartRateVariabilityMilliseconds. Definitions differ, so providers are never mixed.",
    "heart_rate": "Per-day statistics of heart rate samples (bpm). The mean is sample-weighted (not "
                  "time-weighted) because sampling density varies with activity and device. Google heart rate is "
                  "imported only around workouts unless full_heart_rate was used, so Google statistics describe "
                  "exercise periods, not whole days.",
    "weight": "Last weight measurement of each day, converted to kilograms. Measurements whose unit is "
              "unknown (e.g. some Takeout files) are excluded.",
}
ADDITIVE = {"steps", "distance", "calories_out"}


def _providers_for(conn, category: str, metrics: tuple[str, ...], start: str, end: str) -> list[dict]:
    placeholders = ",".join("?" for _ in metrics)
    rows = conn.execute(
        "SELECT provider, COUNT(DISTINCT local_date) AS days FROM records WHERE category=? AND metric IN (" +
        placeholders + ") AND status='active' AND local_date BETWEEN ? AND ? GROUP BY provider ORDER BY days DESC",
        (category, *metrics, start, end)).fetchall()
    return [dict(r) for r in rows]


def _rows(conn, provider: str, category: str, metric: str, start: str, end: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT local_date, value, unit, granularity, data_source, quality, details, start_local, end_local, "
        "start_utc, end_utc, record_key FROM records WHERE provider=? AND category=? AND metric=? AND "
        "status='active' AND local_date BETWEEN ? AND ? AND value IS NOT NULL ORDER BY local_date",
        (provider, category, metric, start, end))]


def _additive_daily(conn, provider, metric, start, end, convert: dict | None, notes: dict) -> dict[str, float]:
    rows = _rows(conn, provider, "activity", metric, start, end)
    sedentary = {r["local_date"]: r["value"] for r in _rows(conn, provider, "activity", "sedentary_minutes", start, end)
                 if r["granularity"] == "daily"}
    daily: dict[str, float] = {}
    intervals: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    def factor(unit):
        if convert is None:
            return 1.0
        if unit not in convert:
            notes["excluded_unknown_unit"] = notes.get("excluded_unknown_unit", 0) + 1
            return None
        return convert[unit]

    for r in rows:
        f = factor(r["unit"])
        if f is None:
            continue
        if r["granularity"] == "daily":
            quality = json.loads(r["quality"]) if r["quality"] else []
            if r["value"] == 0 and "zero_may_mean_no_data" in quality and \
                    (sedentary.get(r["local_date"]) is None or sedentary[r["local_date"]] >= 1440):
                notes["excluded_no_wear_days"] = notes.get("excluded_no_wear_days", 0) + 1
                continue
            daily[r["local_date"]] = r["value"] * f
        else:
            intervals[r["local_date"]][r["data_source"] or "unspecified"].append(r["value"] * f)
    for day, sources in intervals.items():
        if day in daily:
            notes["interval_days_superseded_by_daily_total"] = notes.get("interval_days_superseded_by_daily_total", 0) + 1
            continue
        best = max(sources.items(), key=lambda kv: (len(kv[1]), kv[0]))
        daily[day] = sum(best[1])
        if len(sources) > 1:
            notes["days_with_multiple_sources_one_used"] = notes.get("days_with_multiple_sources_one_used", 0) + 1
    return daily


def _interval(r: dict) -> tuple[datetime | None, datetime | None]:
    start = parse_rfc3339(r["start_utc"]) or (datetime.fromisoformat(r["start_local"]) if r["start_local"] else None)
    end = parse_rfc3339(r["end_utc"]) or (datetime.fromisoformat(r["end_local"]) if r["end_local"] else None)
    if start is not None and end is not None and (start.tzinfo is None) != (end.tzinfo is None):
        return None, None
    return start, end


def _sleep_daily(conn, provider, start, end, notes) -> tuple[dict[str, float], dict[str, float]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in _rows(conn, provider, "sleep", "minutes_asleep", start, end):
        by_day[r["local_date"]].append(r)
    total, main = {}, {}
    for day, sessions in by_day.items():
        accepted: list[tuple[Any, Any, float]] = []
        for r in sorted(sessions, key=lambda x: -x["value"]):
            s, e = _interval(r)
            overlap = any(s and e and a and b and s < b and a < e for a, b, _ in accepted)
            if overlap:
                notes["overlapping_sessions_removed"] = notes.get("overlapping_sessions_removed", 0) + 1
                continue
            accepted.append((s, e, r["value"]))
        total[day] = sum(v for _, _, v in accepted)
        main[day] = max(v for _, _, v in accepted)
    return total, main


def _one_per_day(conn, provider, category, metric, start, end, notes, convert=None, last=False) -> dict[str, float]:
    rows = _rows(conn, provider, category, metric, start, end)
    source_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        source_counts[r["data_source"] or "unspecified"] += 1
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["local_date"]].append(r)
    out = {}
    for day, items in by_day.items():
        if convert is not None:
            usable = [r for r in items if r["unit"] in convert]
            notes["excluded_unknown_unit"] = notes.get("excluded_unknown_unit", 0) + len(items) - len(usable)
            items = usable
            if not items:
                continue
        if last:
            chosen = max(items, key=lambda r: (r["start_utc"] or r["start_local"] or "", r["record_key"]))
        else:
            chosen = max(items, key=lambda r: (source_counts[r["data_source"] or "unspecified"], r["record_key"]))
        if len(items) > 1:
            notes["days_with_multiple_values_one_used"] = notes.get("days_with_multiple_values_one_used", 0) + 1
        out[day] = chosen["value"] * (convert[chosen["unit"]] if convert else 1.0)
    return out


def _heart_rate_daily(conn, provider, start, end) -> tuple[dict[str, float], dict[str, dict]]:
    rows = conn.execute(
        "SELECT local_date, AVG(value) AS mean, MIN(value) AS min, MAX(value) AS max, COUNT(*) AS n FROM records "
        "WHERE provider=? AND category='heart' AND metric='heart_rate' AND status='active' AND value IS NOT NULL "
        "AND local_date BETWEEN ? AND ? GROUP BY local_date", (provider, start, end)).fetchall()
    return {r["local_date"]: r["mean"] for r in rows}, {r["local_date"]: dict(r) for r in rows}


def _bucket(day: str, period: str) -> str:
    d = date.fromisoformat(day)
    if period == "day":
        return day
    if period == "week":
        monday = d - timedelta(days=d.weekday())
        return "week of " + monday.isoformat()
    return d.strftime("%Y-%m")


def _trend(series: dict[str, float]) -> dict[str, Any]:
    if len(series) < 6:
        return {"available": False, "reason": "fewer than 6 days with data"}
    days = sorted(series)
    origin = date.fromisoformat(days[0])
    xs = [(date.fromisoformat(d) - origin).days for d in days]
    ys = [series[d] for d in days]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator if denominator else 0.0
    half = len(ys) // 2
    return {"available": True, "method": "ordinary least squares on days with data (gaps are not filled)",
            "slope_per_week": round(slope * 7, 3), "first_half_mean": round(statistics.fmean(ys[:half]), 2),
            "second_half_mean": round(statistics.fmean(ys[half:]), 2), "days_used": len(ys),
            "caution": "Descriptive only; not adjusted for seasonality, device changes or missing days."}


def summarize(store: Store, metric: str, start_date: str | None, end_date: str | None, period: str = "week",
              provider: str | None = None, today: date | None = None) -> dict[str, Any]:
    if metric not in DEFINITIONS:
        raise ValueError("metric must be one of: " + ", ".join(sorted(DEFINITIONS)))
    if period not in ("day", "week", "month"):
        raise ValueError("period must be day, week or month")
    category, metrics = {
        "steps": ("activity", ("steps",)), "distance": ("activity", ("distance",)),
        "calories_out": ("activity", ("calories_out",)), "sleep": ("sleep", ("minutes_asleep",)),
        "resting_heart_rate": ("heart", ("resting_heart_rate",)),
        "hrv": ("heart", ("hrv_daily_rmssd", "hrv_average")), "heart_rate": ("heart", ("heart_rate",)),
        "weight": ("body", ("weight",)),
    }[metric]
    end = parse_date(end_date, "end_date")
    start = parse_date(start_date, "start_date")
    with store.connect() as conn:
        if end is None:
            latest = conn.execute("SELECT MAX(local_date) FROM records WHERE category=? AND status='active'",
                                  (category,)).fetchone()[0]
            end = date.fromisoformat(latest) if latest else (today or date.today())
        start = start or end - timedelta(days=29)
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        if period == "day" and (end - start).days > 400:
            raise ValueError("Use period 'week' or 'month' for ranges longer than 400 days.")
        s, e = start.isoformat(), end.isoformat()
        candidates = _providers_for(conn, category, metrics, s, e)
        if provider:
            chosen = provider
        elif candidates:
            chosen = candidates[0]["provider"]
        else:
            return {"metric": metric, "start_date": s, "end_date": e, "days_with_data": 0,
                    "message": "No local records for this metric in the range. Data may not be imported yet, "
                               "may be denied, or may not exist; see list_data_types.",
                    "definition": DEFINITIONS[metric]}
        notes: dict[str, int] = {}
        extra: dict[str, Any] = {}
        unit = None
        if metric == "steps":
            series, unit = _additive_daily(conn, chosen, "steps", s, e, None, notes), "steps"
        elif metric == "distance":
            series, unit = _additive_daily(conn, chosen, "distance", s, e, KM, notes), "km"
        elif metric == "calories_out":
            series, unit = _additive_daily(conn, chosen, "calories_out", s, e, {"kcal": 1.0}, notes), "kcal"
        elif metric == "sleep":
            series, main = _sleep_daily(conn, chosen, s, e, notes)
            unit = "minutes asleep"
            extra["main_sleep_mean_minutes"] = round(statistics.fmean(main.values()), 1) if main else None
        elif metric == "resting_heart_rate":
            series, unit = _one_per_day(conn, chosen, "heart", "resting_heart_rate", s, e, notes), "bpm"
        elif metric == "hrv":
            name = "hrv_daily_rmssd" if chosen.startswith("fitbit") else "hrv_average"
            series, unit = _one_per_day(conn, chosen, "heart", name, s, e, notes), "ms"
            extra["provider_metric"] = name
        elif metric == "heart_rate":
            series, stats = _heart_rate_daily(conn, chosen, s, e)
            unit = "bpm"
            if stats:
                extra["overall_min"] = min(v["min"] for v in stats.values())
                extra["overall_max"] = max(v["max"] for v in stats.values())
                extra["samples"] = sum(v["n"] for v in stats.values())
        else:
            series, unit = _one_per_day(conn, chosen, "body", "weight", s, e, notes, convert=KG, last=True), "kg"
    buckets: dict[str, list[float]] = defaultdict(list)
    calendar_days: dict[str, int] = defaultdict(int)
    for d in daterange(start, end):
        calendar_days[_bucket(d.isoformat(), period)] += 1
    for day, value in sorted(series.items()):
        buckets[_bucket(day, period)].append(value)
    periods = []
    for label in sorted(calendar_days):
        values = buckets.get(label, [])
        item: dict[str, Any] = {"period": label, "days_with_data": len(values), "calendar_days": calendar_days[label]}
        if values:
            item.update(mean=round(statistics.fmean(values), 2), min=round(min(values), 2), max=round(max(values), 2))
            if metric in ADDITIVE:
                item["total_of_days_with_data"] = round(sum(values), 2)
        periods.append(item)
    values = list(series.values())
    overall: dict[str, Any] = {"days_with_data": len(values), "calendar_days": (end - start).days + 1,
                               "coverage_pct": round(100 * len(values) / ((end - start).days + 1), 1)}
    if values:
        overall.update(mean=round(statistics.fmean(values), 2), median=round(statistics.median(values), 2),
                       min=round(min(values), 2), max=round(max(values), 2))
    return {"metric": metric, "unit": unit, "provider_used": chosen,
            "other_providers_with_data": [c for c in candidates if c["provider"] != chosen],
            "start_date": s, "end_date": e, "period": period, "overall": overall, "periods": periods,
            "trend": _trend(series), "definition": DEFINITIONS[metric], "exclusions": notes, **extra,
            "caveats": ["Means are over days with data only; missing days are not treated as zero.",
                        "Dates are the provider's calendar dates (account/device time zone)."]}
