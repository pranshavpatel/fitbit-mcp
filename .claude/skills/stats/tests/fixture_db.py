"""Synthetic fitbit-mcp database for tests. Every value is generated; no real health data."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]          # repo root (…/fitbit-mcp)
sys.path.insert(0, str(ROOT / "src"))
try:
    from fitbit_mcp.store import SCHEMA            # the real schema, so drift breaks these tests
except Exception:                                   # pragma: no cover - the package should import
    SCHEMA = None

FIXTURE_DAY = date(2026, 10, 8)
STAMP = "2026-10-08T15:00:00Z"


class Writer:
    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.n = conn, 0

    def payload(self, obj: dict) -> int:
        js = json.dumps(obj, sort_keys=True)
        sha = hashlib.sha256(js.encode()).hexdigest()
        row = self.conn.execute("SELECT id FROM payloads WHERE sha256=?", (sha,)).fetchone()
        if row:
            return row[0]
        return self.conn.execute("INSERT INTO payloads (sha256, json) VALUES (?, ?)", (sha, js)).lastrowid

    def rec(self, category, metric, granularity, local_date, value=None, *, value_text=None, unit=None,
            start_local=None, end_local=None, start_utc=None, end_utc=None, source_id=None, details=None,
            data_source=None, payload=None, provider="google"):
        self.n += 1
        pid = self.payload(payload) if payload is not None else None
        self.conn.execute(
            "INSERT INTO records (provider, category, metric, granularity, record_key, local_date, start_local, end_local, "
            "start_utc, end_utc, utc_offset_seconds, time_basis, value, value_text, unit, source_id, data_source, details, "
            "payload_id, parser, status, fingerprint, first_seen, last_seen, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (provider, category, metric, granularity, "k{}".format(self.n), local_date, start_local, end_local,
             start_utc, end_utc, -14400, "utc_with_offset" if start_utc else "date_only", value, value_text, unit,
             source_id, json.dumps(data_source) if data_source else None, json.dumps(details or {"parser": "test"}),
             pid, "test", "active", "f{}".format(self.n), STAMP, STAMP, STAMP))


def _utc(local: datetime) -> str:
    return (local + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loc(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def create(path: Path, *, day: date = FIXTURE_DAY, days: int = 70, seed: int = 7, empty: bool = False,
           minimal: bool = False) -> Path:
    """Write a fixture DB. `empty`: schema only. `minimal`: steps only (no HRV, sleep or workouts)."""
    conn = sqlite3.connect(path)
    if SCHEMA:
        conn.executescript(SCHEMA)
    if empty:
        conn.commit()
        conn.close()
        return path
    rnd = random.Random(seed)
    w = Writer(conn)
    dev = {"device": {"displayName": "Test Band"}, "platform": "FITBIT", "recordingMethod": "DERIVED"}
    for i in range(days, -1, -1):
        d = day - timedelta(days=i)
        x = d.isoformat()
        steps = None if i % 11 == 5 else rnd.randint(4000, 16000) if i else 900
        if steps is not None:
            w.rec("activity", "steps", "daily", x, steps, unit="count")
            w.rec("activity", "distance", "daily", x, steps * 780.0, unit="mm")
            w.rec("activity", "floors", "daily", x, rnd.randint(2, 20), unit="count")
            w.rec("activity", "calories_out", "daily", x, 2000 + steps * 0.05, unit="kcal")
            w.rec("activity", "active_energy", "daily", x, steps * 0.05, unit="kcal")
            w.rec("activity", "active_minutes_moderate", "daily", x, rnd.randint(5, 40), unit="min")
            w.rec("activity", "active_minutes_vigorous", "daily", x, rnd.randint(0, 30), unit="min")
            w.rec("activity", "hr_zone_light_minutes", "daily", x, rnd.randint(600, 900), unit="min")
            w.rec("activity", "sedentary_minutes", "interval", x, rnd.randint(300, 600), unit="min",
                  start_local=x + "T09:00:00", end_local=x + "T17:00:00")
        if minimal:
            continue
        if i % 13 != 3:
            w.rec("heart", "resting_heart_rate", "daily", x, 58 + rnd.gauss(0, 2), unit="bpm", data_source=dev)
            w.rec("heart", "hrv_average", "daily", x, 80 + rnd.gauss(0, 9), unit="ms", data_source=dev)
            w.rec("spo2_breathing_temperature", "breathing_rate", "daily", x, 16 + rnd.gauss(0, 0.4), unit="breaths/min")
            w.rec("spo2_breathing_temperature", "spo2_avg", "daily", x, 96.5 + rnd.gauss(0, 0.5), unit="%")
            w.rec("spo2_breathing_temperature", "skin_temperature_nightly", "daily", x, 33.4 + rnd.gauss(0, 0.4), unit="degC")
            w.rec("spo2_breathing_temperature", "skin_temperature_baseline", "daily", x, 33.4, unit="degC")
            w.rec("spo2_breathing_temperature", "skin_temperature_relative_stddev_30d", "daily", x, 0.5, unit="degC")
            w.rec("cardio_fitness", "vo2max", "daily", x, 52 + i * -0.02, unit="mL/kg/min")
            w.rec("cardio_fitness", "cardio_fitness_level", "daily", x, value_text="GOOD")
        # night (dated by wake day); every 9th night is missing
        if i % 9 != 4:
            onset = datetime(d.year, d.month, d.day) - timedelta(minutes=rnd.randint(-90, 80))
            length = rnd.randint(330, 560)
            wake = onset + timedelta(minutes=length)
            stages, t, deep, rem, light, awake, kinds = [], onset, 0, 0, 0, 0, ["LIGHT", "DEEP", "LIGHT", "REM", "AWAKE"]
            k = 0
            while t < wake:
                kind = kinds[k % len(kinds)]
                dur = min({"LIGHT": 40, "DEEP": 25, "REM": 20, "AWAKE": 5}[kind], int((wake - t).total_seconds() // 60) or 1)
                stages.append({"type": kind, "startTime": _utc(t), "endTime": _utc(t + timedelta(minutes=dur)),
                               "startUtcOffset": "-14400s", "endUtcOffset": "-14400s"})
                deep += dur if kind == "DEEP" else 0
                rem += dur if kind == "REM" else 0
                light += dur if kind == "LIGHT" else 0
                awake += dur if kind == "AWAKE" else 0
                t += timedelta(minutes=dur)
                k += 1
            payload = {"sleep": {"stages": stages, "shortAwakenings": [{}] * 3, "metadata": {"mainSleep": True},
                                 "summary": {"stagesSummary": [{"type": "AWAKE", "count": str(k // 5), "minutes": str(awake)}]}}}
            sid = "sleep/{}".format(x)
            for metric, v in (("minutes_asleep", deep + rem + light), ("minutes_awake", awake),
                              ("time_in_sleep_period", length), ("stage_deep_minutes", deep), ("stage_rem_minutes", rem),
                              ("stage_light_minutes", light), ("stage_awake_minutes", awake)):
                w.rec("sleep", metric, "session", x, v, unit="min", start_local=_loc(onset), end_local=_loc(wake),
                      start_utc=_utc(onset), end_utc=_utc(wake), source_id=sid, data_source=dev, payload=payload,
                      details={"is_main_sleep": True, "parser": "test"})
        if i == 2:   # an afternoon nap
            a = datetime(d.year, d.month, d.day, 15, 0)
            w.rec("sleep", "minutes_asleep", "session", x, 25, unit="min", start_local=_loc(a),
                  end_local=_loc(a + timedelta(minutes=30)), start_utc=_utc(a), end_utc=_utc(a + timedelta(minutes=30)),
                  source_id="nap/" + x, details={"is_main_sleep": False, "parser": "test"})
        # workouts every other day, rotating types; one duplicate to exercise de-duplication
        if i % 2 == 0 and i > 0:
            etype = ["STRENGTH_TRAINING", "RUNNING", "SOCCER", "CARDIO_WORKOUT"][(i // 2) % 4]
            start = datetime(d.year, d.month, d.day, 17, 30)
            mins = 45 if etype != "SOCCER" else 90
            end = start + timedelta(minutes=mins)
            summ = {"heartRateZoneDurations": {"lightTime": "600s", "moderateTime": "1200s", "vigorousTime": "600s",
                                               "peakTime": "120s"}}
            if etype == "RUNNING":
                summ["distanceMillimeters"] = 8000000
            sources = [("ex/" + x, etype, "ACTIVELY_MEASURED")]
            if i == 4:
                sources.append(("ex-dup/" + x, "WORKOUT", "PASSIVELY_MEASURED"))
            for sid, t_, method in sources:
                payload = {"exercise": {"exerciseType": t_, "metricsSummary": summ}}
                ds = {"platform": "FITBIT", "recordingMethod": method}
                vals = [("duration", mins), ("active_duration", mins), ("average_heart_rate", 135), ("calories", mins * 8),
                        ("active_zone_minutes", 30)]
                if etype == "RUNNING":
                    vals.append(("distance", 8000000))
                for metric, v in vals:
                    w.rec("exercise", metric, "session", x, v, start_local=_loc(start), end_local=_loc(end),
                          start_utc=_utc(start), end_utc=_utc(end), source_id=sid, data_source=ds, payload=payload,
                          details={"exercise_type": t_, "parser": "test"})
            if i <= 14:
                t = start
                while t < end:
                    w.rec("heart", "heart_rate", "sample", x, 120 + 40 * rnd.random(), unit="bpm", start_local=_loc(t),
                          start_utc=_utc(t))
                    t += timedelta(seconds=4)
            for m_ in range(30):
                zone = "CARDIO" if m_ % 3 else "FAT_BURN"
                tm = start + timedelta(minutes=m_)
                w.rec("activity", "active_zone_minutes", "interval", x, 2 if zone == "CARDIO" else 1,
                      start_local=_loc(tm), end_local=_loc(tm + timedelta(minutes=1)),
                      details={"heart_rate_zone": zone, "parser": "test"})
    if not minimal:
        for zone, lo, hi in (("light", 30, 111), ("moderate", 112, 139), ("vigorous", 140, 174), ("peak", 175, 220)):
            w.rec("heart", "hr_zone_{}_min_bpm".format(zone), "daily", (day - timedelta(days=1)).isoformat(), lo)
            w.rec("heart", "hr_zone_{}_max_bpm".format(zone), "daily", (day - timedelta(days=1)).isoformat(), hi)
        for k, kg in enumerate((70.0, 70.3, 70.5, 70.9)):
            dd = day - timedelta(days=42 - k * 14)
            w.rec("body", "weight", "sample", dd.isoformat(), kg * 1000, unit="g", start_local=dd.isoformat() + "T08:00:00")
        w.rec("body", "height", "sample", (day - timedelta(days=60)).isoformat(), 1800, unit="mm",
              start_local=(day - timedelta(days=60)).isoformat() + "T08:00:00")
        x = day.isoformat()
        for hour in range(7, 12):
            for minute in range(0, 60, 2):
                t = datetime(day.year, day.month, day.day, hour, minute)
                lvl = "LIGHTLY_ACTIVE" if minute % 4 else "SEDENTARY"
                w.rec("activity", "activity_level_minutes", "interval", x, 1, start_local=_loc(t),
                      end_local=_loc(t + timedelta(minutes=1)), payload={"activityLevel": {"activityLevelType": lvl}})
        for metric, v, unit in (("calories_in", 650, "kcal"), ("protein", 40, "g"), ("carbohydrate", 70, "g"), ("fat", 20, "g")):
            w.rec("nutrition", metric, "interval", x, v, unit=unit, start_local=x + "T12:00:00", end_local=x + "T12:30:00")
        w.rec("nutrition", "water", "interval", x, 750, unit="mL", start_local=x + "T10:00:00", end_local=x + "T10:00:00")
    conn.execute("INSERT INTO sync_state (provider, last_synced_through, last_sync_at) VALUES ('google', ?, ?)",
                 (day.isoformat(), STAMP))
    conn.execute("INSERT INTO category_status (provider, grp, category, status, reason, updated_at) VALUES "
                 "('google', 'symptoms', 'symptoms_and_mood', 'scope_not_granted', 'Permission not granted', ?)", (STAMP,))
    conn.execute("INSERT INTO category_status (provider, grp, category, status, reason, updated_at) VALUES "
                 "('google', 'body-fat', 'body', 'empty', 'No data found', ?)", (STAMP,))
    conn.commit()
    conn.close()
    return path


CONFIG = {
    "bedtime_goal": "23:30", "bedtime_step": "00:45", "wake_anchor": "08:30", "steps_goal": 10000, "gym_goal": 4,
    "split": ["chest/tri", "back/bi", "legs/abs", "arms"], "split_anchor": {"date": "2026-09-20", "day": "back/bi"},
    "experiment": {"name": "Test experiment", "start": "2026-10-01", "nights": 14, "lights_out": "00:45"},
}
