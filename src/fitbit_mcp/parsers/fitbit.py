"""Category-aware parsers for legacy Fitbit Web API responses (Accept-Language: en_US).

Fitbit range/time-series endpoints report calendar dates in the account's time zone
and contain no UTC offset, so those records use time_basis 'date_only' / 'local'.
Fitbit returns 0 for days without device data in several series; such zeros are kept
but flagged 'zero_may_mean_no_data' so summaries can avoid treating no-wear as activity.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..util import parse_rfc3339, to_number, to_utc_z
from .base import DATE_ONLY, LOCAL, UTC_WITH_OFFSET, ParseResult, Rec

PARSER = "fitbit_v1"

# resource -> (metric, unit)
ACTIVITY_SERIES = {
    "steps": ("steps", "count"), "distance": ("distance", "mi"), "floors": ("floors", "count"),
    "elevation": ("elevation", "ft"), "calories": ("calories_out", "kcal"),
    "activityCalories": ("activity_calories", "kcal"), "caloriesBMR": ("calories_bmr", "kcal"),
    "minutesSedentary": ("sedentary_minutes", "min"), "minutesLightlyActive": ("lightly_active_minutes", "min"),
    "minutesFairlyActive": ("fairly_active_minutes", "min"), "minutesVeryActive": ("very_active_minutes", "min"),
}
INTRADAY_UNITS = {"steps": "count", "calories": "kcal", "distance": "mi", "floors": "count", "elevation": "ft"}


def _slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(text)).strip("_") or "unknown"


def _daily(category: str, metric: str, unit: str | None, day: str, value: Any, group: str,
           payload: Any = None, zero_flag: bool = False) -> Rec | None:
    number = to_number(value)
    text = None
    if number is None:
        if value in (None, ""):
            return None
        text = str(value)[:200]
    quality = ["zero_may_mean_no_data"] if zero_flag and number == 0 else []
    return Rec(category=category, metric=metric, granularity="daily", record_key=day, value=number,
               value_text=text, unit=unit, local_date=day, time_basis=DATE_ONLY, quality=quality,
               payload=payload, scope_key="fitbit:{}:{}".format(group, day))


def _series(payload: dict, key: str) -> list:
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, list) else []


def parse(group: str, payload: Any, context: dict | None = None) -> ParseResult:
    context = context or {}
    out = ParseResult()
    handler = _HANDLERS.get(group)
    if handler is None:
        for prefix, prefixed in _PREFIX_HANDLERS:
            if group.startswith(prefix):
                return prefixed(group, payload, context)
        out.unsupported.append((group, "No parser for Fitbit group '{}'; raw response retained".format(group)))
        return out
    try:
        return handler(group, payload, context)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        out.unsupported.append((group, "Unexpected response shape ({}); raw response retained"
                                .format(type(error).__name__)))
        return out


def _activity_range(group: str, payload: Any, context: dict) -> ParseResult:
    resource = group[len("activity-"):]
    metric, unit = ACTIVITY_SERIES[resource]
    out = ParseResult()
    items = _series(payload, "activities-" + resource)
    if not items and isinstance(payload, dict) and payload:
        out.unsupported.append((group, "Expected key activities-{} not found".format(resource)))
    for item in items:
        rec = _daily("activity", metric, unit, item["dateTime"], item.get("value"), group,
                     zero_flag=resource in ("steps", "distance", "floors", "elevation", "minutesLightlyActive",
                                            "minutesFairlyActive", "minutesVeryActive"))
        if rec:
            if resource == "minutesSedentary" and rec.value is not None and rec.value >= 1440:
                rec.quality.append("full_day_sedentary_suggests_no_wear")
            out.records.append(rec)
    return out


def _heart_range(group: str, payload: Any, context: dict) -> ParseResult:
    out = ParseResult()
    for item in _series(payload, "activities-heart"):
        day, value = item["dateTime"], item.get("value") or {}
        if value.get("restingHeartRate") is not None:   # absent = no data, never zero-filled
            rec = _daily("heart", "resting_heart_rate", "bpm", day, value["restingHeartRate"], group, payload=item)
            if rec:
                out.records.append(rec)
        for zone in value.get("heartRateZones", []) or []:
            if zone.get("minutes") is None:
                continue
            rec = _daily("heart", "hr_zone_{}_minutes".format(_slug(zone.get("name", "zone"))), "min", day,
                         zone["minutes"], group, zero_flag=True)
            if rec:
                rec.details = {"min_bpm": zone.get("min"), "max_bpm": zone.get("max")}
                out.records.append(rec)
    return out


def _value_dict_series(key: str, category: str, fields: dict[str, tuple[str, str]]):
    def handler(group: str, payload: Any, context: dict) -> ParseResult:
        out = ParseResult()
        items = payload if isinstance(payload, list) else _series(payload, key)
        if isinstance(payload, dict) and key not in payload and "dateTime" in payload:
            items = [payload]   # single-day spo2 shape
        for item in items:
            value = item.get("value") or {}
            for field, (metric, unit) in fields.items():
                if isinstance(value, dict) and value.get(field) is not None:
                    rec = _daily(category, metric, unit, item["dateTime"], value[field], group, payload=item)
                    if rec:
                        out.records.append(rec)
        return out
    return handler


def _vo2max(group: str, payload: Any, context: dict) -> ParseResult:
    out = ParseResult()
    for item in _series(payload, "cardioScore"):
        raw = (item.get("value") or {}).get("vo2Max")
        if raw is None:
            continue
        rec = _daily("cardio_fitness", "vo2max", "mL/kg/min", item["dateTime"], raw, group, payload=item)
        if rec and rec.value is None and isinstance(raw, str) and "-" in raw:
            low, _, high = raw.partition("-")
            rec.details = {"range_low": to_number(low), "range_high": to_number(high)}
            rec.quality.append("reported_as_range")
        if rec:
            out.records.append(rec)
    return out


def _logs(key: str, fields: dict[str, tuple[str, str]], category: str):
    def handler(group: str, payload: Any, context: dict) -> ParseResult:
        out = ParseResult()
        for item in _series(payload, key):
            day, clock = item.get("date"), item.get("time")
            log_id = item.get("logId")
            for field, (metric, unit) in fields.items():
                if item.get(field) is None:
                    continue
                key_id = str(log_id) if log_id is not None else "{}T{}".format(day, clock)
                out.records.append(Rec(
                    category=category, metric=metric, granularity="sample", record_key=key_id,
                    value=to_number(item[field]), unit=unit, local_date=day,
                    start_local="{}T{}".format(day, clock) if clock else None, time_basis=LOCAL,
                    source_id=str(log_id) if log_id is not None else None,
                    data_source={"source": item.get("source")} if item.get("source") else None,
                    payload=item, scope_key="fitbit:{}:{}".format(group, day)))
        return out
    return handler


def _sleep(group: str, payload: Any, context: dict) -> ParseResult:
    out = ParseResult()
    for item in _series(payload, "sleep"):
        log_id, day = str(item["logId"]), item.get("dateOfSleep")
        common = dict(category="sleep", granularity="session", record_key=log_id, local_date=day,
                      start_local=(item.get("startTime") or "")[:19] or None,
                      end_local=(item.get("endTime") or "")[:19] or None, time_basis=LOCAL, source_id=log_id,
                      payload=item, scope_key="fitbit:sleep:{}".format(day),
                      details={"is_main_sleep": bool(item.get("isMainSleep")), "type": item.get("type"),
                               "attributed_to": "dateOfSleep (wake date)"})
        for field, metric, unit in [("minutesAsleep", "minutes_asleep", "min"),
                                    ("minutesAwake", "minutes_awake", "min"),
                                    ("timeInBed", "time_in_bed", "min"),
                                    ("minutesToFallAsleep", "minutes_to_fall_asleep", "min"),
                                    ("efficiency", "efficiency", "%")]:
            if item.get(field) is not None:
                out.records.append(Rec(metric=metric, value=to_number(item[field]), unit=unit, **common))
        summary = ((item.get("levels") or {}).get("summary") or {})
        for stage, stats in summary.items():
            if isinstance(stats, dict) and stats.get("minutes") is not None:
                out.records.append(Rec(metric="stage_{}_minutes".format(_slug(stage)),
                                       value=to_number(stats["minutes"]), unit="min", **common))
    return out


def _azm(group: str, payload: Any, context: dict) -> ParseResult:
    out = ParseResult()
    for item in _series(payload, "activities-active-zone-minutes"):
        for field, value in (item.get("value") or {}).items():
            metric = "azm_total" if field == "activeZoneMinutes" else "azm_" + _slug(field.replace(
                "ActiveZoneMinutes", ""))
            rec = _daily("activity", metric, "min", item["dateTime"], value, group, payload=item)
            if rec:
                out.records.append(rec)
    return out


def _food_series(key: str, metric: str, unit: str):
    def handler(group: str, payload: Any, context: dict) -> ParseResult:
        out = ParseResult()
        for item in _series(payload, key):
            rec = _daily("nutrition", metric, unit, item["dateTime"], item.get("value"), group, zero_flag=True)
            if rec:
                if rec.value == 0:
                    rec.quality = ["zero_means_nothing_logged"]
                out.records.append(rec)
        return out
    return handler


def _temperature_skin(group: str, payload: Any, context: dict) -> ParseResult:
    out = ParseResult()
    for item in _series(payload, "tempSkin"):
        value = (item.get("value") or {}).get("nightlyRelative")
        rec = _daily("spo2_breathing_temperature", "skin_temperature_nightly_relative",
                     "relative_deg_en_US_units", item["dateTime"], value, group, payload=item)
        if rec:
            rec.quality.append("relative_to_personal_baseline")
            out.records.append(rec)
    return out


def _intraday(group: str, payload: Any, context: dict) -> ParseResult:
    out = ParseResult()
    resource = group[len("intraday-"):]
    day = context.get("date")
    if resource == "heart":
        dataset = (payload.get("activities-heart-intraday") or {}).get("dataset", [])
        for point in dataset:
            moment = "{}T{}".format(day, point["time"])
            out.records.append(Rec(category="heart", metric="heart_rate", granularity="sample", record_key=moment,
                                   value=to_number(point.get("value")), unit="bpm", local_date=day,
                                   start_local=moment, time_basis=LOCAL,
                                   scope_key="fitbit:{}:{}".format(group, day)))
        return out
    unit = INTRADAY_UNITS.get(resource)
    if unit is None:
        out.unsupported.append((group, "Intraday resource not parsed; raw response retained"))
        return out
    metric = "calories_out" if resource == "calories" else resource
    dataset = (payload.get("activities-{}-intraday".format(resource)) or {}).get("dataset", [])
    for point in dataset:
        start = datetime.fromisoformat("{}T{}".format(day, point["time"]))
        out.records.append(Rec(category="activity", metric=metric, granularity="interval",
                               record_key=start.isoformat(), value=to_number(point.get("value")), unit=unit,
                               local_date=day, start_local=start.isoformat(),
                               end_local=(start + timedelta(minutes=1)).isoformat(), time_basis=LOCAL,
                               quality=["intraday_minute"], scope_key="fitbit:{}:{}".format(group, day)))
    return out


def _workouts(group: str, payload: Any, context: dict) -> ParseResult:
    out = ParseResult()
    for item in payload.get("activities", []) or []:
        log_id = item.get("logId")
        if log_id is None:
            out.unsupported.append((group, "Workout without logId; raw response retained"))
            continue
        moment = parse_rfc3339(item.get("startTime"))
        offset = int(moment.utcoffset().total_seconds()) if moment and moment.utcoffset() is not None else None
        start_local = moment.replace(tzinfo=None).isoformat() if moment else None
        duration_ms = to_number(item.get("duration"))
        end_local = None
        if moment and duration_ms is not None:
            end_local = (moment.replace(tzinfo=None) + timedelta(milliseconds=duration_ms)).isoformat()
        common = dict(category="exercise", granularity="session", record_key=str(log_id),
                      local_date=start_local[:10] if start_local else None, start_local=start_local,
                      end_local=end_local, start_utc=to_utc_z(moment), utc_offset_seconds=offset,
                      time_basis=UTC_WITH_OFFSET if offset is not None else LOCAL, source_id=str(log_id),
                      data_source={"logType": item.get("logType"), "source": (item.get("source") or {}).get("name")},
                      payload=item, details={"activity_name": item.get("activityName")})
        distance_unit = {"Mile": "mi", "Kilometer": "km"}.get(item.get("distanceUnit"), item.get("distanceUnit"))
        for field, metric, unit, scale in [("duration", "duration", "min", 1 / 60000),
                                           ("activeDuration", "active_duration", "min", 1 / 60000),
                                           ("calories", "calories", "kcal", 1), ("steps", "steps", "count", 1),
                                           ("distance", "distance", distance_unit or "unknown", 1),
                                           ("averageHeartRate", "average_heart_rate", "bpm", 1)]:
            number = to_number(item.get(field))
            if number is not None:
                rec = Rec(metric=metric, value=number * scale, unit=unit, **common)
                if field == "distance" and not distance_unit:
                    rec.quality.append("unit_unknown")
                out.records.append(rec)
    return out


_HANDLERS = {
    "heart": _heart_range,
    "hrv": _value_dict_series("hrv", "heart", {"dailyRmssd": ("hrv_daily_rmssd", "ms"),
                                                "deepRmssd": ("hrv_deep_rmssd", "ms")}),
    "spo2": _value_dict_series("spo2", "spo2_breathing_temperature",
                               {"avg": ("spo2_avg", "%"), "min": ("spo2_min", "%"), "max": ("spo2_max", "%")}),
    "breathing": _value_dict_series("br", "spo2_breathing_temperature",
                                    {"breathingRate": ("breathing_rate", "breaths/min")}),
    "temperature-skin": _temperature_skin,
    "vo2max": _vo2max,
    "sleep": _sleep,
    # Weight logs also carry 'fat'; it is taken from the fat endpoint only, to avoid double counting.
    "weight": _logs("weight", {"weight": ("weight", "lb"), "bmi": ("bmi", "kg/m2")}, "body"),
    "fat": _logs("fat", {"fat": ("body_fat", "%")}, "body"),
    "zone-minutes": _azm,
    "water": _food_series("foods-log-water", "water", "fl_oz"),
    "calories-in": _food_series("foods-log-caloriesIn", "calories_in", "kcal"),
    "workouts": _workouts,
}
_PREFIX_HANDLERS = [("activity-", _activity_range), ("intraday-", _intraday)]
