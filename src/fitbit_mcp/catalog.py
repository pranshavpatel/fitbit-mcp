"""Provider endpoint catalog.

Scopes, Google data types and Fitbit snapshot/intraday endpoints are ported from the
original exporter's catalog.py (checked against provider documentation 2026-10-07).
Fitbit *range* endpoints are an improvement over the exporter's one-request-per-day
plan: they cut a multi-year import from tens of thousands of requests to hundreds,
which matters under the 150 requests/hour legacy quota and the October 30, 2026 shutdown.
"""
from __future__ import annotations

from datetime import date

FITBIT_API = "https://api.fitbit.com"
GOOGLE_API = "https://health.googleapis.com"

FITBIT_AUTHORIZE = "https://www.fitbit.com/oauth2/authorize"
FITBIT_TOKEN = "https://api.fitbit.com/oauth2/token"
FITBIT_REVOKE = "https://api.fitbit.com/oauth2/revoke"
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"

# Google announced the legacy Fitbit Web API is turned off on this date.
FITBIT_SHUTDOWN = date(2026, 10, 30)

# Fitbit's Accept-Language decides units. en_US is kept from the original exporter;
# the unit map below is tied to it and recorded as provenance on every raw file.
FITBIT_LOCALE = "en_US"
FITBIT_UNITS_EN_US = {
    "distance": "mi", "elevation": "ft", "weight": "lb", "water": "fl_oz",
}

FITBIT_SCOPES = ("activity cardio_fitness electrocardiogram heartrate irregular_rhythm_notifications "
                 "location nutrition oxygen_saturation profile respiratory_rate settings sleep social "
                 "temperature weight").split()

GOOGLE_SCOPE_NAMES = ("activity_and_fitness health_metrics_and_measurements location nutrition sleep "
                      "reproductive_health logged_symptoms mindfulness ecg irn profile settings").split()
GOOGLE_SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth."


def google_scope(name: str) -> str:
    return GOOGLE_SCOPE_PREFIX + name + ".readonly"


GOOGLE_SCOPES = [google_scope(s) for s in GOOGLE_SCOPE_NAMES]

# All 42 members of the documented v4 DataPoint union (from the exporter).
GOOGLE_TYPES = """
steps floors heart-rate sleep daily-resting-heart-rate daily-heart-rate-variability
exercise weight altitude distance body-fat active-zone-minutes heart-rate-variability
daily-sleep-temperature-derivations sedentary-period run-vo2-max oxygen-saturation
daily-oxygen-saturation activity-level vo2-max daily-vo2-max nutrition-log
irregular-rhythm-notification electrocardiogram daily-heart-rate-zones hydration-log
food time-in-heart-rate-zone active-minutes respiratory-rate-sleep-summary
daily-respiratory-rate swim-lengths-data height basal-energy-burned
core-body-temperature active-energy-burned food-measurement-unit blood-glucose
menstrual-period ovulation-test symptoms moods
""".split()

# Google per-type retrieval rules (filters, chunking, roll-ups) live in google_registry.py,
# ported from health-coach-app where they were verified against a live account.

# ------------------------------------------------------------------- Fitbit

# Snapshots (current account state). Raw responses are retained; not indexed as records.
FITBIT_SNAPSHOTS = [
    ("profile", "profile", "/1/user/-/profile.json"),
    ("badges", "profile", "/1/user/-/badges.json"),
    ("devices", "settings", "/1/user/-/devices.json"),
    ("lifetime", "activity", "/1/user/-/activities.json"),
    ("activity-goals-daily", "activity", "/1/user/-/activities/goals/daily.json"),
    ("activity-goals-weekly", "activity", "/1/user/-/activities/goals/weekly.json"),
    ("weight-goal", "weight", "/1/user/-/body/log/weight/goal.json"),
    ("sleep-goal", "sleep", "/1.2/user/-/sleep/goal.json"),
    ("food-goal", "nutrition", "/1/user/-/foods/log/goal.json"),
    ("water-goal", "nutrition", "/1/user/-/foods/log/water/goal.json"),
    ("irn-profile", "irregular_rhythm_notifications", "/1/user/-/irn/profile.json"),
]

# Range endpoints: (group, category, scope, path template, max days per request).
# Maximum ranges are the documented limits; smaller chunks are always accepted.
_ACTIVITY_RESOURCES = ["steps", "distance", "floors", "elevation", "calories", "activityCalories",
                       "caloriesBMR", "minutesSedentary", "minutesLightlyActive", "minutesFairlyActive",
                       "minutesVeryActive"]
FITBIT_RANGES = [
    ("activity-" + r, "activity", "activity", "/1/user/-/activities/" + r + "/date/{start}/{end}.json", 1095)
    for r in _ACTIVITY_RESOURCES
] + [
    ("heart", "heart", "heartrate", "/1/user/-/activities/heart/date/{start}/{end}.json", 365),
    ("hrv", "heart", "heartrate", "/1/user/-/hrv/date/{start}/{end}.json", 30),
    ("sleep", "sleep", "sleep", "/1.2/user/-/sleep/date/{start}/{end}.json", 100),
    ("weight", "body", "weight", "/1/user/-/body/log/weight/date/{start}/{end}.json", 31),
    ("fat", "body", "weight", "/1/user/-/body/log/fat/date/{start}/{end}.json", 31),
    ("spo2", "spo2_breathing_temperature", "oxygen_saturation", "/1/user/-/spo2/date/{start}/{end}.json", 30),
    ("breathing", "spo2_breathing_temperature", "respiratory_rate", "/1/user/-/br/date/{start}/{end}.json", 30),
    ("temperature-skin", "spo2_breathing_temperature", "temperature",
     "/1/user/-/temp/skin/date/{start}/{end}.json", 30),
    ("vo2max", "cardio_fitness", "cardio_fitness", "/1/user/-/cardioscore/date/{start}/{end}.json", 30),
    ("zone-minutes", "activity", "activity",
     "/1/user/-/activities/active-zone-minutes/date/{start}/{end}.json", 1095),
    ("water", "nutrition", "nutrition", "/1/user/-/foods/log/water/date/{start}/{end}.json", 1095),
    ("calories-in", "nutrition", "nutrition", "/1/user/-/foods/log/caloriesIn/date/{start}/{end}.json", 1095),
]

# Intraday: one request per day (ported from the exporter). Opt-in because of quota cost.
FITBIT_INTRADAY = [
    ("intraday-heart", "heart", "heartrate", "/1/user/-/activities/heart/date/{date}/1d/1sec.json"),
    ("intraday-steps", "activity", "activity", "/1/user/-/activities/steps/date/{date}/1d/1min.json"),
    ("intraday-calories", "activity", "activity", "/1/user/-/activities/calories/date/{date}/1d/1min.json"),
    ("intraday-distance", "activity", "activity", "/1/user/-/activities/distance/date/{date}/1d/1min.json"),
    ("intraday-floors", "activity", "activity", "/1/user/-/activities/floors/date/{date}/1d/1min.json"),
    ("intraday-elevation", "activity", "activity", "/1/user/-/activities/elevation/date/{date}/1d/1min.json"),
]

# Paginated lists: (group, category, scope, path, page limit).
FITBIT_LISTS = [
    ("workouts", "exercise", "activity", "/1/user/-/activities/list.json", 100),
    ("ecg", "ecg", "electrocardiogram", "/1/user/-/ecg/list.json", 10),
    ("irn", "irn", "irregular_rhythm_notifications", "/1/user/-/irn/alerts/list.json", 10),
]

FITBIT_CATEGORIES = sorted({c for _, c, _, _, _ in FITBIT_RANGES} | {c for _, c, _, _ in FITBIT_INTRADAY}
                           | {c for _, c, _, _, _ in FITBIT_LISTS})
from .google_registry import CATEGORIES as GOOGLE_CATEGORIES  # noqa: E402
ALL_IMPORT_CATEGORIES = sorted(set(FITBIT_CATEGORIES) | set(GOOGLE_CATEGORIES))
