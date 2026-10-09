"""Per-type Google Health API v4 knowledge, ported from health-coach-app's registry.

The first 23 specs (``verified=True``) mirror health-coach-app, whose filter fields,
time kinds, page sizes, chunk sizes and rollup behaviour were verified against a live
account:

* filter fields are snake_case paths (``heart_rate.sample_time.physical_time``);
* CIVIL filters take local wall-clock ``YYYY-MM-DDT00:00:00``; PHYSICAL filters take that
  same local midnight converted to UTC; DATE filters take ``YYYY-MM-DD``; upper bounds
  are exclusive;
* sleep and exercise cap pageSize at 25;
* steps, distance, floors, energy, total calories, active minutes and time in HR zones
  are read through ``POST …:dailyRollUp`` (a read-only query), max 14 days per request;
* heart rate (sampled every 1–3 s, ~37 MB/day) is fetched only over exercise windows
  unless whole days are explicitly requested.

The remaining types (``verified=False``) follow the same naming convention but were not
verified live; they are fetched only on request, and a rejected filter is reported.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

CIVIL, PHYSICAL, DATE = "civil", "physical", "date"
ECG_LOWER_BOUND, NO_FILTER = "ecg_lower_bound", "none"
LIST, DAILY_ROLLUP = "list", "daily-rollup"
KEY_NAME_ID, KEY_DAILY_DATE, KEY_SAMPLE_TIME, KEY_INTERVAL, KEY_ROLLUP = (
    "name_id", "daily_date", "sample_time", "interval", "rollup_date")
SCOPE_RANGE, SCOPE_EXERCISE = "range", "exercise"


def camel(kind: str) -> str:
    head, *rest = kind.split("-")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


@dataclass(frozen=True)
class Spec:
    id: str
    payload_key: str
    operation: str
    key_strategy: str
    category: str
    scope_name: str                  # Google OAuth scope short name
    filter_field: str = ""
    filter_kind: str = DATE
    page_size: int = 1000
    max_chunk_days: int = 14
    day_from: str = "start"          # sleep belongs to the day you woke up
    discriminator: str = ""
    scope: str = SCOPE_RANGE
    window_pad_s: int = 120
    verified: bool = True
    catalog: bool = False            # shared reference catalog (not personal data): never imported


_A, _H, _S, _N = "activity_and_fitness", "health_metrics_and_measurements", "sleep", "nutrition"

SPECS_LIST: tuple[Spec, ...] = (
    Spec("sleep", "sleep", LIST, KEY_NAME_ID, "sleep", _S, "sleep.interval.civil_end_time", CIVIL,
         page_size=25, day_from="end"),
    Spec("exercise", "exercise", LIST, KEY_NAME_ID, "exercise", _A, "exercise.interval.civil_start_time", CIVIL,
         page_size=25),
    Spec("daily-resting-heart-rate", "dailyRestingHeartRate", LIST, KEY_DAILY_DATE, "heart", _H,
         "daily_resting_heart_rate.date", DATE, max_chunk_days=90),
    Spec("daily-heart-rate-variability", "dailyHeartRateVariability", LIST, KEY_DAILY_DATE, "heart", _H,
         "daily_heart_rate_variability.date", DATE, max_chunk_days=90),
    Spec("daily-oxygen-saturation", "dailyOxygenSaturation", LIST, KEY_DAILY_DATE, "spo2_breathing_temperature",
         _H, "daily_oxygen_saturation.date", DATE, max_chunk_days=90),
    Spec("daily-respiratory-rate", "dailyRespiratoryRate", LIST, KEY_DAILY_DATE, "spo2_breathing_temperature", _H,
         "daily_respiratory_rate.date", DATE, max_chunk_days=90),
    Spec("daily-vo2-max", "dailyVo2Max", LIST, KEY_DAILY_DATE, "cardio_fitness", _A, "daily_vo2_max.date", DATE,
         max_chunk_days=90),
    Spec("daily-sleep-temperature-derivations", "dailySleepTemperatureDerivations", LIST, KEY_DAILY_DATE,
         "spo2_breathing_temperature", _H, "daily_sleep_temperature_derivations.date", DATE, max_chunk_days=90),
    Spec("respiratory-rate-sleep-summary", "respiratoryRateSleepSummary", LIST, KEY_SAMPLE_TIME,
         "spo2_breathing_temperature", _H, "respiratory_rate_sleep_summary.sample_time.physical_time", PHYSICAL,
         max_chunk_days=30),
    Spec("heart-rate-variability", "heartRateVariability", LIST, KEY_SAMPLE_TIME, "heart", _H,
         "heart_rate_variability.sample_time.physical_time", PHYSICAL, page_size=10000, max_chunk_days=7),
    Spec("oxygen-saturation", "oxygenSaturation", LIST, KEY_SAMPLE_TIME, "spo2_breathing_temperature", _H,
         "oxygen_saturation.sample_time.physical_time", PHYSICAL, page_size=10000, max_chunk_days=7),
    Spec("heart-rate", "heartRate", LIST, KEY_SAMPLE_TIME, "heart", _H, "heart_rate.sample_time.physical_time",
         PHYSICAL, page_size=10000, max_chunk_days=3, scope=SCOPE_EXERCISE),
    Spec("weight", "weight", LIST, KEY_SAMPLE_TIME, "body", _H, "weight.sample_time.physical_time", PHYSICAL,
         max_chunk_days=90),
    Spec("active-zone-minutes", "activeZoneMinutes", LIST, KEY_INTERVAL, "activity", _A,
         "active_zone_minutes.interval.civil_start_time", CIVIL, page_size=10000, max_chunk_days=14,
         discriminator="heartRateZone"),
    Spec("activity-level", "activityLevel", LIST, KEY_INTERVAL, "activity", _A,
         "activity_level.interval.civil_start_time", CIVIL, page_size=10000, max_chunk_days=7,
         discriminator="activityLevelType"),
    Spec("sedentary-period", "sedentaryPeriod", LIST, KEY_INTERVAL, "activity", _A,
         "sedentary_period.interval.civil_start_time", CIVIL, page_size=10000, max_chunk_days=14),
    Spec("steps", "steps", DAILY_ROLLUP, KEY_ROLLUP, "activity", _A),
    Spec("distance", "distance", DAILY_ROLLUP, KEY_ROLLUP, "activity", _A),
    Spec("floors", "floors", DAILY_ROLLUP, KEY_ROLLUP, "activity", _A),
    Spec("active-energy-burned", "activeEnergyBurned", DAILY_ROLLUP, KEY_ROLLUP, "activity", _A),
    Spec("total-calories", "totalCalories", DAILY_ROLLUP, KEY_ROLLUP, "activity", _A),
    Spec("active-minutes", "activeMinutes", DAILY_ROLLUP, KEY_ROLLUP, "activity", _A),
    Spec("time-in-heart-rate-zone", "timeInHeartRateZone", DAILY_ROLLUP, KEY_ROLLUP, "activity", _A),
)


# The remaining types. Their time shape (interval / sample / session / daily), payload fields and
# OAuth scope come from the Google Health v4 discovery document cached by the ghealth CLI
# (revision 20260904), not from a live run, so they are marked verified=False. If the API rejects
# the primary filter form, the importer tries the other documented forms (see filter_variants).
_IV, _SM, _DD = ".interval.civil_start_time", ".sample_time.physical_time", ".date"
_M, _L, _R, _E, _I = "mindfulness", "logged_symptoms", "reproductive_health", "ecg", "irn"


def _doc(type_id: str, payload_key: str, key: str, category: str, scope: str, filter_kind: str,
         field_suffix: str, **extra) -> Spec:
    field = type_id.replace("-", "_") + field_suffix if field_suffix else ""
    extra.setdefault("max_chunk_days", 30)
    return Spec(type_id, payload_key, extra.pop("operation", LIST), key, category, scope, field, filter_kind,
                verified=False, **extra)


SPECS_LIST += (
    # interval types
    _doc("altitude", "altitude", KEY_INTERVAL, "activity", _A, CIVIL, _IV, page_size=10000),
    _doc("basal-energy-burned", "basalEnergyBurned", KEY_INTERVAL, "activity", _A, CIVIL, _IV, page_size=10000),
    _doc("swim-lengths-data", "swimLengthsData", KEY_INTERVAL, "activity", _A, CIVIL, _IV, page_size=10000),
    _doc("menstrual-period", "menstrualPeriod", KEY_NAME_ID, "reproductive_health", _R, CIVIL, _IV),
    # daily
    _doc("daily-heart-rate-zones", "dailyHeartRateZones", KEY_DAILY_DATE, "heart", _H, DATE, _DD, max_chunk_days=90),
    # roll-up only (listed on the data types page; absent from the DataPoint union)
    _doc("calories-in-heart-rate-zone", "caloriesInHeartRateZone", KEY_ROLLUP, "activity", _A, DATE, "",
         operation=DAILY_ROLLUP, max_chunk_days=14),
    # samples
    _doc("vo2-max", "vo2Max", KEY_SAMPLE_TIME, "cardio_fitness", _A, PHYSICAL, _SM, max_chunk_days=90),
    _doc("run-vo2-max", "runVo2Max", KEY_SAMPLE_TIME, "cardio_fitness", _A, PHYSICAL, _SM, max_chunk_days=90),
    _doc("body-fat", "bodyFat", KEY_SAMPLE_TIME, "body", _H, PHYSICAL, _SM, max_chunk_days=90),
    _doc("height", "height", KEY_SAMPLE_TIME, "body", _H, PHYSICAL, _SM, max_chunk_days=90),
    _doc("core-body-temperature", "coreBodyTemperature", KEY_SAMPLE_TIME, "spo2_breathing_temperature", _H,
         PHYSICAL, _SM),
    _doc("blood-glucose", "bloodGlucose", KEY_SAMPLE_TIME, "blood_glucose", _H, PHYSICAL, _SM),
    _doc("ovulation-test", "ovulationTest", KEY_SAMPLE_TIME, "reproductive_health", _R, PHYSICAL, _SM,
         max_chunk_days=90),
    _doc("symptoms", "symptoms", KEY_SAMPLE_TIME, "symptoms_and_mood", _L, PHYSICAL, _SM, max_chunk_days=90),
    _doc("moods", "moods", KEY_SAMPLE_TIME, "symptoms_and_mood", _M, PHYSICAL, _SM, max_chunk_days=90),
    # sessions
    _doc("nutrition-log", "nutritionLog", KEY_NAME_ID, "nutrition", _N, CIVIL, _IV),
    _doc("hydration-log", "hydrationLog", KEY_NAME_ID, "nutrition", _N, CIVIL, _IV),
    _doc("irregular-rhythm-notification", "irregularRhythmNotification", KEY_NAME_ID, "irn", _I, CIVIL, _IV,
         max_chunk_days=90),
    # ECG: only `electrocardiogram.interval.start_time >= ...` is supported, so one open-ended request
    _doc("electrocardiogram", "electrocardiogram", KEY_NAME_ID, "ecg", _E, ECG_LOWER_BOUND, ".interval.start_time",
         page_size=25, max_chunk_days=36500),
    # `food` and `food-measurement-unit` list Google's shared food catalog (public foods, A-Z, many
    # thousands of pages), not your data; your own food entries are in nutrition-log. Never imported.
    _doc("food", "food", KEY_NAME_ID, "nutrition", _N, NO_FILTER, "", catalog=True),
    _doc("food-measurement-unit", "foodMeasurementUnit", KEY_NAME_ID, "nutrition", _N, NO_FILTER, "",
         catalog=True),
)

SPECS: dict[str, Spec] = {s.id: s for s in SPECS_LIST}
VERIFIED = [s for s in SPECS_LIST if s.verified]
CATEGORIES = sorted({s.category for s in SPECS_LIST})
VERIFIED_CATEGORIES = sorted({s.category for s in VERIFIED})


def specs_for(categories: list[str] | None, include_unverified: bool, full_heart_rate: bool) -> list[Spec]:
    """Specs in dependency order: exercise before exercise-scoped heart rate.

    All documented types are included by default; include_unverified=False limits a run to the
    23 types health-coach-app verified against the live API.
    """
    chosen = [spec for spec in SPECS_LIST
              if not spec.catalog and (not categories or spec.category in categories)
              and (spec.verified or include_unverified)]
    if full_heart_rate:
        chosen = [replace(s, scope=SCOPE_RANGE) if s.scope == SCOPE_EXERCISE else s for s in chosen]
    return sorted(chosen, key=lambda s: s.scope == SCOPE_EXERCISE)


def filter_variants(spec: Spec) -> list[tuple[str, str]]:
    """(field, kind) forms to try, primary first. Live-verified types only use their verified form."""
    if spec.filter_kind in (NO_FILTER, ECG_LOWER_BOUND) or spec.verified:
        return [(spec.filter_field, spec.filter_kind)]
    base = spec.id.replace("-", "_")
    if spec.filter_kind == PHYSICAL:      # sample types
        return [(spec.filter_field, PHYSICAL), (base + ".sample_time.civil_time", CIVIL)]
    if spec.filter_kind == CIVIL:         # interval and session types
        return [(spec.filter_field, CIVIL), (base + ".interval.start_time", PHYSICAL)]
    return [(spec.filter_field, spec.filter_kind)]


def local_midnight_utc(day: date, tz) -> str:
    local = datetime(day.year, day.month, day.day, tzinfo=tz)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_filter(spec: Spec, start: date, end: date, tz, variant: int = 0) -> str | None:
    """Filter covering civil days start..end inclusive (the API's upper bound is exclusive)."""
    field, kind = filter_variants(spec)[variant]
    if kind == NO_FILTER:
        return None
    if kind == ECG_LOWER_BOUND:   # upper bound applied locally after parsing
        return '{} >= "{}"'.format(field, local_midnight_utc(start, tz))
    spec = replace(spec, filter_field=field, filter_kind=kind)
    stop = end + timedelta(days=1)
    if spec.filter_kind == DATE:
        lo, hi = start.isoformat(), stop.isoformat()
    elif spec.filter_kind == CIVIL:
        lo, hi = start.isoformat() + "T00:00:00", stop.isoformat() + "T00:00:00"
    else:
        lo, hi = local_midnight_utc(start, tz), local_midnight_utc(stop, tz)
    return '{f} >= "{lo}" AND {f} < "{hi}"'.format(f=spec.filter_field, lo=lo, hi=hi)


def build_instant_filter(spec: Spec, start_utc: str, end_utc: str) -> str:
    return '{f} >= "{lo}" AND {f} < "{hi}"'.format(f=spec.filter_field, lo=start_utc, hi=end_utc)


def rollup_body(start: date, end: date, page_token: str | None = None) -> dict:
    stop = end + timedelta(days=1)
    body = {"range": {"start": {"date": {"year": start.year, "month": start.month, "day": start.day}},
                      "end": {"date": {"year": stop.year, "month": stop.month, "day": stop.day}}},
            "windowSizeDays": 1}
    if page_token:
        body["pageToken"] = page_token
    return body
