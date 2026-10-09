"""Parsers for Google Health API v4 data points and daily roll-ups.

Identity and civil-day rules are ported from health-coach-app's projection.py, which was
checked against a live account:

* sessions (sleep, exercise) are keyed by the resource-name id; sleep belongs to the day
  you woke up (civil end date);
* daily summaries carry no resource id, so they are keyed by date + data source;
* samples are keyed by UTC instant + data source;
* intervals are keyed by start | end | discriminator (e.g. heart-rate zone) | data source;
* roll-up rows are keyed by their civil start date.

Restated values therefore update the same record instead of creating a new one.
Field maps for the 23 live-verified types follow health-coach-app's SQL views. Other types
use a generic parser that keeps the original object and infers units only from Google's
documented field-name suffixes. ``"NaN"`` strings (e.g. skin-temperature baselines before
30 days of history) are treated as missing, never as numbers.
"""
from __future__ import annotations

import re
from datetime import timedelta, timezone
from typing import Any

from .. import google_registry as reg
from ..util import canonical_json, civil_to_str, parse_offset_seconds, parse_rfc3339, sha256_bytes, to_number, \
    to_utc_z
from .base import DATE_ONLY, LOCAL, UTC_WITH_OFFSET, ParseResult, Rec

PARSER = "google_v4"
GENERIC = "google_v4_generic"

_UNIT_SUFFIXES = [
    ("MillimetersPerSecond", "mm/s"), ("SecondsPerMeter", "s/m"), ("MilligramsPerDeciliter", "mg/dL"),
    ("MillimolesPerLiter", "mmol/L"), ("BreathsPerMinute", "breaths/min"), ("BeatsPerMinute", "bpm"),
    ("Milliseconds", "ms"), ("Millimeters", "mm"), ("Milliliters", "mL"), ("Kilocalories", "kcal"),
    ("Kcal", "kcal"), ("Grams", "g"), ("Percentage", "%"), ("Percent", "%"), ("Celsius", "degC"),
    ("Minutes", "min"), ("Seconds", "s"), ("Meters", "m"), ("Liters", "L"),
]
_TIME_FIELDS = {"interval", "sampleTime", "date", "createTime", "updateTime", "metadata"}

# (payload path, metric, unit). Paths use dots for nesting.
FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "steps": [("countSum", "steps", "count"), ("count", "steps", "count")],
    "floors": [("countSum", "floors", "count"), ("count", "floors", "count")],
    "distance": [("millimetersSum", "distance", "mm"), ("millimeters", "distance", "mm")],
    "active-energy-burned": [("kcalSum", "active_energy", "kcal"), ("kcal", "active_energy", "kcal")],
    "basal-energy-burned": [("kcalSum", "basal_energy", "kcal"), ("kcal", "basal_energy", "kcal")],
    "altitude": [("gainMillimetersSum", "altitude_gain", "mm"), ("gainMillimeters", "altitude_gain", "mm")],
    "swim-lengths-data": [("strokeCount", "swim_strokes", "count")],
    "body-fat": [("percentage", "body_fat", "%")],
    "core-body-temperature": [("temperatureCelsius", "core_temperature", "degC")],
    "blood-glucose": [("bloodGlucoseMilligramsPerDeciliter", "blood_glucose", "mg/dL")],
    "vo2-max": [("vo2Max", "vo2max", "mL/kg/min")],
    "run-vo2-max": [("runVo2Max", "run_vo2max", "mL/kg/min")],
    "electrocardiogram": [("beatsPerMinuteAvg", "ecg_average_heart_rate", "bpm")],
    "hydration-log": [("amountConsumed.milliliters", "water", "mL")],
    "nutrition-log": [("energy.kcal", "calories_in", "kcal"), ("totalFat.grams", "fat", "g"),
                      ("totalCarbohydrate.grams", "carbohydrate", "g"), ("energyFromFat.kcal", "calories_from_fat",
                                                                         "kcal")],
    "total-calories": [("kcalSum", "calories_out", "kcal")],
    "heart-rate": [("beatsPerMinute", "heart_rate", "bpm")],
    "daily-resting-heart-rate": [("beatsPerMinute", "resting_heart_rate", "bpm")],
    "daily-heart-rate-variability": [
        ("averageHeartRateVariabilityMilliseconds", "hrv_average", "ms"),
        ("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds", "hrv_deep_sleep_rmssd", "ms"),
        ("nonRemHeartRateBeatsPerMinute", "non_rem_heart_rate", "bpm"), ("entropy", "hrv_entropy", "unitless")],
    "heart-rate-variability": [("rootMeanSquareOfSuccessiveDifferencesMilliseconds", "hrv_rmssd", "ms"),
                               ("standardDeviationMilliseconds", "hrv_sdnn", "ms")],
    "daily-oxygen-saturation": [("averagePercentage", "spo2_avg", "%"), ("lowerBoundPercentage", "spo2_lower", "%"),
                                ("upperBoundPercentage", "spo2_upper", "%"),
                                ("standardDeviationPercentage", "spo2_stddev", "%")],
    "oxygen-saturation": [("percentage", "spo2", "%")],
    "daily-respiratory-rate": [("breathsPerMinute", "breathing_rate", "breaths/min")],
    "respiratory-rate-sleep-summary": [
        ("fullSleepStats.breathsPerMinute", "breathing_rate_full_sleep", "breaths/min"),
        ("deepSleepStats.breathsPerMinute", "breathing_rate_deep_sleep", "breaths/min"),
        ("lightSleepStats.breathsPerMinute", "breathing_rate_light_sleep", "breaths/min"),
        ("remSleepStats.breathsPerMinute", "breathing_rate_rem_sleep", "breaths/min")],
    "daily-vo2-max": [("vo2Max", "vo2max", "mL/kg/min"), ("vo2MaxCovariance", "vo2max_covariance", "unitless")],
    "daily-sleep-temperature-derivations": [
        ("nightlyTemperatureCelsius", "skin_temperature_nightly", "degC"),
        ("baselineTemperatureCelsius", "skin_temperature_baseline", "degC"),
        ("relativeNightlyStddev30dCelsius", "skin_temperature_relative_stddev_30d", "degC")],
    "weight": [("weightGrams", "weight", "g")],
    "height": [("heightMillimeters", "height", "mm")],
    "active-zone-minutes": [("activeZoneMinutes", "active_zone_minutes", "min")],
}


def _join(values: Any) -> str:
    return ",".join(str(v) for v in values) if isinstance(values, list) else str(values or "")


# Types whose content is categorical; stored as text records (original object kept in full).
TEXT_TYPES = {
    "moods": lambda b: "moods={}; valences={}".format(_join(b.get("moods")), _join(b.get("valences"))),
    "symptoms": lambda b: _join(b.get("symptoms")),
    "ovulation-test": lambda b: str(b.get("result") or ""),
    "menstrual-period": lambda b: "menstrual period",
    "food": lambda b: " / ".join(str(x) for x in (b.get("displayName"), b.get("brand")) if x),
    "food-measurement-unit": lambda b: str(b.get("displayName") or ""),
}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def infer_unit(field: str) -> str | None:
    if field == "count":
        return "count"
    capitalized = field[:1].upper() + field[1:]   # 'millimeters' and 'distanceMillimeters' both match
    for suffix, unit in _UNIT_SUFFIXES:
        if capitalized.endswith(suffix):
            return unit
    return None


def _path(payload: dict, path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def source_key(point: dict) -> str:
    """Stable 'platform/device/recordingMethod' label (as in health-coach-app)."""
    ds = point.get("dataSource")
    if not isinstance(ds, dict):
        return ""
    parts = [str(ds.get("platform") or "?")]
    device = (ds.get("device") or {}).get("displayName") if isinstance(ds.get("device"), dict) else None
    if device:
        parts.append(str(device))
    if ds.get("recordingMethod"):
        parts.append(str(ds["recordingMethod"]))
    return "/".join(parts)


def _time(kind: str, body: dict, day_from: str) -> dict[str, Any]:
    info: dict[str, Any] = {"time_basis": LOCAL}
    if isinstance(body.get("interval"), dict):
        interval = body["interval"]
        start, end = parse_rfc3339(interval.get("startTime")), parse_rfc3339(interval.get("endTime"))
        offset = parse_offset_seconds(interval.get("startUtcOffset"))
        end_offset = parse_offset_seconds(interval.get("endUtcOffset"))
        start_day, start_civil = civil_to_str(interval.get("civilStartTime"))
        end_day, end_civil = civil_to_str(interval.get("civilEndTime"))
        if start is not None and offset is not None and start_civil is None:
            local = start.astimezone(timezone(timedelta(seconds=offset)))
            start_civil, start_day = local.replace(tzinfo=None).isoformat(), local.date().isoformat()
        if end is not None and (end_offset if end_offset is not None else offset) is not None and end_civil is None:
            local = end.astimezone(timezone(timedelta(seconds=end_offset if end_offset is not None else offset)))
            end_civil, end_day = local.replace(tzinfo=None).isoformat(), local.date().isoformat()
        info.update(start_utc=to_utc_z(start), end_utc=to_utc_z(end), utc_offset_seconds=offset,
                    start_local=start_civil, end_local=end_civil,
                    local_date=end_day if day_from == "end" else start_day)
        if start is not None and offset is not None:
            info["time_basis"] = UTC_WITH_OFFSET
    elif isinstance(body.get("sampleTime"), dict):
        sample = body["sampleTime"]
        moment = parse_rfc3339(sample.get("physicalTime"))
        offset = parse_offset_seconds(sample.get("utcOffset"))
        day, civil = civil_to_str(sample.get("civilTime"))
        if moment is not None and offset is not None and civil is None:
            local = moment.astimezone(timezone(timedelta(seconds=offset)))
            civil, day = local.replace(tzinfo=None).isoformat(), local.date().isoformat()
        info.update(start_utc=to_utc_z(moment), utc_offset_seconds=offset, start_local=civil, local_date=day)
        if moment is not None and offset is not None:
            info["time_basis"] = UTC_WITH_OFFSET
    elif isinstance(body.get("date"), dict):
        day, _ = civil_to_str(body["date"])
        info.update(local_date=day, time_basis=DATE_ONLY)
    return info


def _spec(kind: str) -> reg.Spec:
    spec = reg.SPECS.get(kind)
    if spec is None:  # unknown type (e.g. a newer API type in a Takeout file)
        spec = reg.Spec(kind, reg.camel(kind), reg.LIST, reg.KEY_NAME_ID, kind, "", verified=False)
    return spec


def parse_datapoint(kind: str, point: dict, provider_tag: str = "google") -> ParseResult:
    out = ParseResult()
    if not isinstance(point, dict):
        out.unsupported.append((kind, "Data point is not an object"))
        return out
    spec = _spec(kind)
    body = point.get(spec.payload_key)
    if not isinstance(body, dict):
        candidates = [k for k, v in point.items() if k not in ("name", "dataSource", "civilStartTime", "civilEndTime")
                      and isinstance(v, dict)]
        if len(candidates) == 1:
            body = point[candidates[0]]
        else:
            out.unsupported.append((kind, "Data point has no recognizable '{}' body".format(spec.payload_key)))
            return out
    src = source_key(point)
    quality: list[str] = []
    is_rollup = "civilStartTime" in point and "name" not in point
    if is_rollup:
        day, _ = civil_to_str(point.get("civilStartTime"))
        times: dict[str, Any] = {"local_date": day, "time_basis": DATE_ONLY}
        key, granularity = day, "daily"
        if day is None:
            out.unsupported.append((kind, "Roll-up row has no civilStartTime"))
            return out
    else:
        times = _time(kind, body, spec.day_from)
        strategy = spec.key_strategy if spec.key_strategy != reg.KEY_ROLLUP else reg.KEY_NAME_ID
        name = point.get("name")
        name_id = name.rsplit("/", 1)[-1].strip() if isinstance(name, str) and "/" in name else None
        if strategy == reg.KEY_NAME_ID:
            key = name_id or "{}|{}".format(times.get("start_utc"), times.get("end_utc"))
        elif strategy == reg.KEY_DAILY_DATE:
            key = "{}|{}".format(times.get("local_date"), src)
        elif strategy == reg.KEY_SAMPLE_TIME:
            key = "{}|{}".format(times.get("start_utc"), src)
        else:
            disc = str(body.get(spec.discriminator, "")) if spec.discriminator else ""
            key = "{}|{}|{}|{}".format(times.get("start_utc"), times.get("end_utc"), disc, src)
        if key.startswith("None|") and not name_id:
            key = "sha256:" + sha256_bytes(canonical_json(point).encode())[:32]
            quality.append("no_stable_identity_key_is_content_hash")
        granularity = ("session" if kind in ("sleep", "exercise", "electrocardiogram") else
                       "interval" if "interval" in body else "sample" if "sampleTime" in body else "daily")
    if times.get("local_date") is None:
        quality.append("no_civil_date")
    # Deletion reconciliation only where the request window is defined in the same civil days.
    reconcilable = provider_tag == "google" and (is_rollup or spec.filter_kind in (reg.CIVIL, reg.DATE))
    scope_key = "google:{}:{}".format(kind, times["local_date"]) if reconcilable and times.get("local_date") else None
    common = dict(category=spec.category, granularity=granularity, record_key=key,
                  source_id=point.get("name") or None, data_source=point.get("dataSource"), payload=point,
                  scope_key=scope_key, **times)
    parser_name = PARSER

    def emit(metric: str, value: Any, unit: str | None, extra: list[str] | None = None,
             value_text: str | None = None, details: dict | None = None):
        number = to_number(value)
        if number is None and value_text is None:
            return
        out.records.append(Rec(metric=metric, value=number, unit=unit, value_text=value_text, details=details,
                               quality=quality + (extra or []), **common))

    if kind in FIELDS:
        for path, metric, unit in FIELDS[kind]:
            emit(metric, _path(body, path), unit,
                 details={"heart_rate_zone": body.get("heartRateZone")} if body.get("heartRateZone") else None)
        if kind == "daily-vo2-max" and body.get("cardioFitnessLevel"):
            emit("cardio_fitness_level", None, None, value_text=str(body["cardioFitnessLevel"]))
        if kind == "electrocardiogram" and body.get("resultClassification"):
            emit("ecg_classification", None, None, value_text=str(body["resultClassification"]))
        if kind == "nutrition-log" and (body.get("foodDisplayName") or body.get("mealType")):
            emit("food_logged", None, None, value_text=" / ".join(
                str(x) for x in (body.get("foodDisplayName"), body.get("mealType")) if x))
        if not out.records:
            out.unsupported.append((kind, "Expected fields missing or not numeric (e.g. 'NaN'); raw retained"))
    elif kind in TEXT_TYPES:
        label = TEXT_TYPES[kind](body)
        if kind == "menstrual-period":
            start, end = parse_rfc3339(times.get("start_utc")), parse_rfc3339(times.get("end_utc"))
            days = (end - start).total_seconds() / 86400.0 if start and end else None
            emit("period_days", days, "days", value_text=label) if days is not None else \
                emit("entry", None, None, value_text=label)
        else:
            emit("entry", None, None, value_text=label or kind)
    elif kind == "irregular-rhythm-notification":
        windows = body.get("alertWindows") or []
        emit("irn_alert_windows", len(windows), "count")
        emit("irn_positive_windows", sum(1 for w in windows if isinstance(w, dict) and w.get("positive")), "count")
    elif kind == "daily-heart-rate-zones":
        for zone in body.get("heartRateZones", []) or []:
            name = str(zone.get("heartRateZoneType", "unknown")).lower()
            emit("hr_zone_{}_min_bpm".format(name), zone.get("minBeatsPerMinute"), "bpm")
            emit("hr_zone_{}_max_bpm".format(name), zone.get("maxBeatsPerMinute"), "bpm")
    elif kind == "calories-in-heart-rate-zone":
        for item in body.get("caloriesInHeartRateZones", []) or []:
            emit("calories_hr_zone_{}".format(str(item.get("heartRateZone", "unknown")).lower()), item.get("kcal"),
                 "kcal")
    elif kind == "active-minutes":
        for item in body.get("activeMinutesRollupByActivityLevel", []) or []:
            emit("active_minutes_" + str(item.get("activityLevel", "unknown")).lower(), item.get("activeMinutesSum"),
                 "min")
    elif kind == "time-in-heart-rate-zone":
        for item in body.get("timeInHeartRateZones", []) or []:
            seconds = parse_offset_seconds(item.get("duration"))
            if seconds is not None:
                emit("hr_zone_{}_minutes".format(str(item.get("heartRateZone", "unknown")).lower()), seconds / 60.0,
                     "min")
    elif kind in ("activity-level", "sedentary-period"):
        start, end = parse_rfc3339(times.get("start_utc")), parse_rfc3339(times.get("end_utc"))
        if start and end:
            metric = "sedentary_minutes" if kind == "sedentary-period" else "activity_level_minutes"
            emit(metric, (end - start).total_seconds() / 60.0, "min",
                 value_text=str(body.get("activityLevelType")) if body.get("activityLevelType") else None)
    elif kind == "sleep":
        summary = body.get("summary") or {}
        metadata = body.get("metadata") or {}
        details = {"type": body.get("type"), "is_main_sleep": bool(metadata.get("mainSleep")),
                   "nap": metadata.get("nap"), "attributed_to": "civil end date (wake date)"}
        for field, metric in [("minutesAsleep", "minutes_asleep"), ("minutesAwake", "minutes_awake"),
                              ("minutesInSleepPeriod", "time_in_sleep_period"),
                              ("minutesToFallAsleep", "minutes_to_fall_asleep"),
                              ("minutesAfterWakeUp", "minutes_after_wake_up")]:
            emit(metric, summary.get(field), "min", details=details)
        for stage in summary.get("stagesSummary", []) or []:
            if isinstance(stage, dict):
                emit("stage_{}_minutes".format(str(stage.get("type", "unknown")).lower()), stage.get("minutes"), "min",
                     details=details)
        if not out.records:
            out.unsupported.append((kind, "Sleep session without summary minutes; raw retained"))
    elif kind == "exercise":
        metrics = body.get("metricsSummary") or {}
        details = {"exercise_type": body.get("exerciseType"), "display_name": body.get("displayName")}
        for field, metric, unit in [("caloriesKcal", "calories", "kcal"), ("distanceMillimeters", "distance", "mm"),
                                    ("steps", "steps", "count"),
                                    ("averageHeartRateBeatsPerMinute", "average_heart_rate", "bpm"),
                                    ("activeZoneMinutes", "active_zone_minutes", "min"),
                                    ("elevationGainMillimeters", "elevation_gain", "mm"),
                                    ("averageSpeedMillimetersPerSecond", "average_speed", "mm/s")]:
            emit(metric, metrics.get(field), unit, details=details)
        for zone, value in (metrics.get("heartRateZoneDurations") or {}).items():
            seconds = parse_offset_seconds(value)
            if seconds is not None:
                emit("hr_zone_{}_minutes".format(snake(zone.replace("Time", ""))), seconds / 60.0, "min",
                     details=details)
        active = parse_offset_seconds(body.get("activeDuration"))
        if active is not None:
            emit("active_duration", active / 60.0, "min", details=details)
        start, end = parse_rfc3339(times.get("start_utc")), parse_rfc3339(times.get("end_utc"))
        if start and end:
            emit("duration", (end - start).total_seconds() / 60.0, "min", details=details)
        if not out.records:
            emit("session", None, None, value_text=str(body.get("exerciseType") or "exercise"), details=details)
    else:
        parser_name = GENERIC
        numeric = 0
        for field, value in body.items():
            if field in _TIME_FIELDS or isinstance(value, (dict, list)):
                continue
            if to_number(value) is None:
                continue
            unit = infer_unit(field)
            emit(snake(field), value, unit or "unknown", ["unit_inferred_from_field_name"] if unit else ["unit_unknown"])
            numeric += 1
        if numeric == 0:
            enums = {k: v for k, v in body.items() if k not in _TIME_FIELDS and isinstance(v, (str, bool))}
            emit("entry", None, None, ["no_numeric_fields"], value_text=canonical_json(enums)[:300] if enums else kind)
    for rec in out.records:
        rec.details = dict(rec.details or {}, parser=parser_name, source=src or None)
    return out


def parse_page(kind: str, payload: Any, provider_tag: str = "google") -> ParseResult:
    out = ParseResult()
    if not isinstance(payload, dict):
        out.unsupported.append((kind, "Response is not an object"))
        return out
    rows = payload.get("rollupDataPoints") if "rollupDataPoints" in payload else payload.get("dataPoints")
    for point in rows or []:
        out.extend(parse_datapoint(kind, point, provider_tag))
    return out
