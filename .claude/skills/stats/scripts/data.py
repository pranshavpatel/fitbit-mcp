"""Read-only data access and model building for the stats dashboard.

Same access pattern as the daily-health-brief skill: the SQLite file in $FITBIT_MCP_HOME
(~/.fitbit-mcp) is opened with mode=ro and only fixed, parameterized queries run. Nothing is
written and nothing leaves the machine. Dates and clock times are in the local timezone (tz.py).

`build_model()` returns plain dicts and lists (JSON-serializable) so the renderer and `--json`
see exactly the same numbers.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tz import local_zone

import coach as CO
import journal as J
import lifts as L
import scores as S

NY = local_zone()      # the display timezone (tz.py); named NY for history
BASELINE_DAYS = 28
WEAR_MIN_MINUTES = 600
LOAD_SPAN = 150   # days of history loaded: 28-day trends whose baselines need 28 days before them, plus slack

DEFAULT_CONFIG = {
    "bedtime_goal": "23:30",
    "bedtime_step": None,
    "wake_anchor": None,
    "steps_goal": 10000,
    "azm_goal": 22,
    "gym_goal": 4,
    "split": ["chest/tri", "back/bi", "legs/abs", "arms"],
    "split_anchor": None,          # {"date": "YYYY-MM-DD", "day": "back/bi"}: what that day's lift was
    "impact_types": ["RUNNING", "TRAIL_RUN", "SOCCER", "TREADMILL_RUNNING"],
    "experiment": None,            # {"name", "start", "nights", "lights_out"}
}

TYPE_LABEL = {"STRENGTH_TRAINING": "Strength", "CARDIO_WORKOUT": "Cardio", "WALKING": "Walk", "RUNNING": "Run",
              "TRAIL_RUN": "Trail run", "SOCCER": "Soccer", "BOXING": "Boxing", "WORKOUT": "Workout",
              "TABLE_TENNIS": "Table tennis", "SPORT": "Sport", "HIKING": "Hike", "BIKING": "Bike",
              "SWIMMING": "Swim", "YOGA": "Yoga", "TREADMILL_RUNNING": "Treadmill"}
STAGE_KEYS = {"DEEP": "deep", "LIGHT": "light", "REM": "rem", "AWAKE": "awake"}


def data_home() -> Path:
    return Path(os.environ.get("FITBIT_MCP_HOME", "~/.fitbit-mcp")).expanduser()


def training_log_path(home: Path | None = None) -> Path:
    return (home or data_home()) / "training_log.json"


def load_training_log(home: Path | None = None) -> dict[str, str]:
    """Date → split day the user tagged (`fitdash tag`). Missing or unreadable file → empty."""
    try:
        data = json.loads(training_log_path(home).read_text())
    except (OSError, ValueError):
        return {}
    sessions = data.get("sessions", data) if isinstance(data, dict) else {}
    return {d: v for d, v in sessions.items() if isinstance(d, str) and isinstance(v, str)}


def load_config(home: Path | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = (home or data_home()) / "coaching" / "stats.json"
    try:
        cfg.update(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass
    cfg["lift_log"] = {**(cfg.get("lift_log") or {}), **load_training_log(home)}
    cfg["lift_sets"] = L.load(home or data_home())
    cfg["journal"] = J.load(home or data_home())
    cfg["habit_list"] = J.habits(cfg)
    cfg["coach_notes"] = CO.load(home or data_home())
    return cfg


# ---------------------------------------------------------------- read-only store

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        self.conn.row_factory = sqlite3.Row

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def close(self) -> None:
        self.conn.close()

    def provider_for(self, category: str, metric: str, granularity: str, start: str, end: str) -> str | None:
        """One provider per metric: whichever has the most rows in the window."""
        rows = self.q("SELECT provider, COUNT(*) AS n FROM records WHERE category=? AND metric=? AND granularity=? "
                      "AND status='active' AND local_date BETWEEN ? AND ? GROUP BY provider ORDER BY n DESC LIMIT 1",
                      (category, metric, granularity, start, end))
        return rows[0]["provider"] if rows else None

    def daily(self, category: str, metric: str, start: str, end: str) -> dict[str, float]:
        prov = self.provider_for(category, metric, "daily", start, end)
        if not prov:
            return {}
        rows = self.q("SELECT local_date, value FROM records WHERE provider=? AND category=? AND metric=? "
                      "AND granularity='daily' AND status='active' AND value IS NOT NULL AND local_date BETWEEN ? AND ? "
                      "ORDER BY local_date, updated_at", (prov, category, metric, start, end))
        return {r["local_date"]: r["value"] for r in rows}

    def daily_text(self, category: str, metric: str, start: str, end: str) -> dict[str, str]:
        rows = self.q("SELECT local_date, value_text FROM records WHERE category=? AND metric=? AND granularity='daily' "
                      "AND status='active' AND value_text IS NOT NULL AND local_date BETWEEN ? AND ? "
                      "ORDER BY local_date, updated_at", (category, metric, start, end))
        return {r["local_date"]: r["value_text"] for r in rows}

    def sessions(self, category: str, start: str, end: str) -> list[dict]:
        prov = self.provider_for(category, "duration" if category == "exercise" else "minutes_asleep",
                                 "session", start, end)
        if not prov:
            return []
        rows = self.q(
            "SELECT r.source_id, r.metric, r.value, r.local_date, r.start_local, r.end_local, r.start_utc, r.end_utc, "
            "r.details, r.data_source, p.json AS payload FROM records r LEFT JOIN payloads p ON p.id = r.payload_id "
            "WHERE r.provider=? AND r.category=? AND r.granularity='session' AND r.status='active' "
            "AND r.local_date BETWEEN ? AND ? ORDER BY r.start_local", (prov, category, start, end))
        out: dict[str, dict] = {}
        for r in rows:
            s = out.get(r["source_id"])
            if s is None:
                s = out[r["source_id"]] = {
                    "date": r["local_date"], "start": r["start_local"], "end": r["end_local"],
                    "start_utc": r["start_utc"], "end_utc": r["end_utc"],
                    "details": _loads(r["details"]), "source": _loads(r["data_source"]), "payload": None}
            if s["payload"] is None and r["payload"]:
                s["payload"] = _loads(r["payload"])
            s[r["metric"]] = r["value"]
        return list(out.values())

    def samples(self, category: str, metric: str, start_local: str, end_local: str) -> list[tuple[str, float]]:
        # the local_date bounds let SQLite use the (category, metric, local_date) index instead of scanning
        # every sample of the metric; a sample's local_date always falls within its own timestamp's dates
        d0 = (date.fromisoformat(start_local[:10]) - timedelta(days=1)).isoformat()
        d1 = (date.fromisoformat(end_local[:10]) + timedelta(days=1)).isoformat()
        rows = self.q("SELECT start_local, value FROM records WHERE category=? AND metric=? AND local_date BETWEEN ? AND ? "
                      "AND granularity='sample' AND status='active' AND value IS NOT NULL AND start_local BETWEEN ? AND ? "
                      "ORDER BY start_local", (category, metric, d0, d1, start_local, end_local))
        return [(r["start_local"], r["value"]) for r in rows]

    def interval_sum(self, category: str, metric: str, start: str, end: str) -> dict[str, float]:
        rows = self.q("SELECT local_date, SUM(value) AS v FROM records WHERE category=? AND metric=? "
                      "AND granularity='interval' AND status='active' AND local_date BETWEEN ? AND ? GROUP BY local_date",
                      (category, metric, start, end))
        return {r["local_date"]: r["v"] for r in rows}

    def minutes_by_day(self, category: str, metric: str, start: str, end: str) -> dict[str, int]:
        rows = self.q("SELECT local_date, COUNT(*) AS n FROM records WHERE category=? AND metric=? AND status='active' "
                      "AND local_date BETWEEN ? AND ? GROUP BY local_date", (category, metric, start, end))
        return {r["local_date"]: r["n"] for r in rows}

    def azm_by_day(self, start: str, end: str) -> dict[str, dict[str, float]]:
        rows = self.q("SELECT local_date, json_extract(details, '$.heart_rate_zone') AS zone, COUNT(*) AS minutes, "
                      "SUM(value) AS points FROM records WHERE category='activity' AND metric='active_zone_minutes' "
                      "AND granularity='interval' AND status='active' AND local_date BETWEEN ? AND ? "
                      "GROUP BY local_date, zone", (start, end))
        out: dict[str, dict[str, float]] = defaultdict(dict)
        for r in rows:
            out[r["local_date"]][r["zone"] or "UNKNOWN"] = r["minutes"]
            out[r["local_date"]]["_points"] = out[r["local_date"]].get("_points", 0) + (r["points"] or 0)
        return dict(out)

    def hr_minutes_by_day(self, start: str, end: str) -> dict[str, int]:
        rows = self.q("SELECT local_date, COUNT(DISTINCT substr(start_local, 1, 16)) AS m FROM records "
                      "WHERE category='heart' AND metric='heart_rate' AND granularity='sample' AND status='active' "
                      "AND local_date BETWEEN ? AND ? GROUP BY local_date", (start, end))
        return {r["local_date"]: r["m"] for r in rows}

    def hr_max_estimate(self, start: str, end: str) -> float | None:
        """99.5th percentile of recorded heart rate: robust to the odd spike, calibrated on your history."""
        n = self.q("SELECT COUNT(*) AS n FROM records WHERE category='heart' AND metric='heart_rate' "
                   "AND status='active' AND local_date BETWEEN ? AND ?", (start, end))[0]["n"]
        if n < 1000:
            return None
        row = self.q("SELECT value FROM records WHERE category='heart' AND metric='heart_rate' AND status='active' "
                     "AND local_date BETWEEN ? AND ? ORDER BY value DESC LIMIT 1 OFFSET ?", (start, end, int(n * 0.005)))
        return row[0]["value"] if row else None

    def activity_levels(self, day: str) -> list[tuple[str, str]]:
        rows = self.q("SELECT r.start_local, json_extract(p.json, '$.activityLevel.activityLevelType') AS lvl "
                      "FROM records r LEFT JOIN payloads p ON p.id = r.payload_id WHERE r.category='activity' "
                      "AND r.metric='activity_level_minutes' AND r.granularity='interval' AND r.status='active' "
                      "AND r.local_date=?", (day,))
        return [(r["start_local"], r["lvl"]) for r in rows if r["start_local"]]

    def weights(self, start: str, end: str) -> list[tuple[str, float]]:
        rows = self.q("SELECT local_date, value, unit FROM records WHERE category='body' AND metric='weight' "
                      "AND status='active' AND value IS NOT NULL AND local_date BETWEEN ? AND ? ORDER BY start_local",
                      (start, end))
        return [(r["local_date"], r["value"] / 1000 if (r["unit"] or "g") == "g" else r["value"]) for r in rows]

    def latest_value(self, category: str, metric: str) -> tuple[str, float, str] | None:
        rows = self.q("SELECT local_date, value, unit FROM records WHERE category=? AND metric=? AND status='active' "
                      "AND value IS NOT NULL ORDER BY local_date DESC LIMIT 1", (category, metric))
        return (rows[0]["local_date"], rows[0]["value"], rows[0]["unit"]) if rows else None

    def last_sync(self) -> tuple[str | None, str | None]:
        rows = self.q("SELECT last_sync_at, last_synced_through FROM sync_state ORDER BY last_sync_at DESC LIMIT 1")
        return (rows[0]["last_sync_at"], rows[0]["last_synced_through"]) if rows else (None, None)

    def category_status(self) -> list[dict]:
        try:
            return [dict(r) for r in self.q("SELECT provider, grp, category, status, reason FROM category_status")]
        except sqlite3.OperationalError:
            return []

    def coverage(self) -> dict:
        """First and last local date per category. Done per (category, metric) so SQLite can answer
        each MIN/MAX straight from the (category, metric, local_date) index instead of a full scan."""
        pairs = self.q("SELECT DISTINCT category, metric FROM records")
        out: dict[str, dict] = {}
        for p in pairs:
            row = self.q("SELECT MIN(local_date) AS first, MAX(local_date) AS last FROM records "
                         "WHERE category=? AND metric=? AND local_date IS NOT NULL", (p["category"], p["metric"]))[0]
            if row["first"] is None:
                continue
            c = out.setdefault(p["category"], {"first": row["first"], "last": row["last"]})
            c["first"], c["last"] = min(c["first"], row["first"]), max(c["last"], row["last"])
        return out

    def metrics(self) -> list[str]:
        return [r["metric"] for r in self.q("SELECT DISTINCT metric FROM records WHERE status='active'")]

    def zone_thresholds(self, day: str) -> dict[str, tuple[float, float]]:
        """The user's Fitbit heart-rate zone limits in effect on `day`."""
        out = {}
        for zone in ("light", "moderate", "vigorous", "peak"):
            lims = []
            for edge in ("min", "max"):
                rows = self.q("SELECT value FROM records WHERE category='heart' AND metric=? AND status='active' "
                              "AND local_date <= ? ORDER BY local_date DESC LIMIT 1",
                              ("hr_zone_{}_{}_bpm".format(zone, edge), day))
                lims.append(rows[0]["value"] if rows else None)
            if None not in lims:
                out[zone] = (lims[0], lims[1])
        return out

    def first_date(self, metrics: tuple[str, ...]) -> str | None:
        marks = ",".join("?" * len(metrics))
        row = self.q("SELECT MIN(local_date) AS d FROM records WHERE status='active' AND metric IN ({})".format(marks), metrics)
        return row[0]["d"] if row else None

    def devices(self, start: str, end: str) -> list[str]:
        rows = self.q("SELECT DISTINCT json_extract(data_source, '$.device.displayName') AS d FROM records "
                      "WHERE status='active' AND local_date BETWEEN ? AND ? AND data_source IS NOT NULL", (start, end))
        return sorted({r["d"] for r in rows if r["d"]})


def open_store(home: Path | None = None) -> Store | None:
    path = (home or data_home()) / "fitbit.sqlite3"
    return Store(path) if path.exists() else None


# ---------------------------------------------------------------- helpers

def _loads(s: str | None) -> dict:
    try:
        return json.loads(s) if s else {}
    except ValueError:
        return {}


def iso(d: date) -> str:
    return d.isoformat()


def span(day: date, n: int) -> list[str]:
    return [iso(day - timedelta(days=i)) for i in range(n - 1, -1, -1)]


def local_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def mins_of_day(s: str | None) -> float | None:
    t = local_dt(s)
    return None if t is None else t.hour * 60 + t.minute + t.second / 60


def hhmm(s: str | None) -> float | None:
    if not s:
        return None
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def epoch(s: str | None) -> float | None:
    """Seconds for ordering and durations. UTC timestamps when present, else local wall time."""
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=NY)
    return t.timestamp()


def to_ny(ts: str | None) -> str | None:
    if not ts:
        return None
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(NY).isoformat(timespec="minutes")


def _parse_ts(stage_time: str, offset: str | None) -> datetime | None:
    try:
        t = datetime.fromisoformat(stage_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    secs = 0
    if offset and offset.endswith("s"):
        try:
            secs = int(float(offset[:-1]))
        except ValueError:
            secs = 0
    return (t + timedelta(seconds=secs)).replace(tzinfo=None)


def mean(vals) -> float | None:
    v = [x for x in vals if x is not None]
    return statistics.fmean(v) if v else None


# ---------------------------------------------------------------- sleep nights

def is_night_start(start_local: str | None) -> bool:
    t = local_dt(start_local)
    return t is not None and (t.hour >= 19 or t.hour < 8)


def nights_by_date(sleeps: list[dict]) -> dict[str, dict]:
    """A night is the longest sleep that started between 7 pm and 8 am, dated by the wake date.
    Everything else that day is a nap."""
    by_date: dict[str, list[dict]] = defaultdict(list)
    for s in sleeps:
        by_date[s["date"]].append(s)
    out = {}
    for d, items in by_date.items():
        cands = [s for s in items if is_night_start(s["start"])] or \
            [s for s in items if s["details"].get("is_main_sleep")]
        main = max(cands, key=lambda s: s.get("minutes_asleep") or 0) if cands else None
        naps = [s for s in items if s is not main]
        out[d] = {"main": main, "naps": naps}
    return out


def stage_timeline(main: dict) -> list[dict]:
    """[{stage, start_min, end_min}] relative to the session start, from the raw sleep payload."""
    sleep = (main.get("payload") or {}).get("sleep") or {}
    t0 = local_dt(main["start"])
    out = []
    for st in sleep.get("stages") or []:
        a = _parse_ts(st.get("startTime"), st.get("startUtcOffset"))
        b = _parse_ts(st.get("endTime"), st.get("endUtcOffset"))
        kind = STAGE_KEYS.get(st.get("type"))
        if a and b and kind and t0:
            out.append({"stage": kind, "start_min": (a - t0).total_seconds() / 60, "end_min": (b - t0).total_seconds() / 60})
    return sorted(out, key=lambda x: x["start_min"])


def awake_counts(main: dict) -> tuple[int | None, int]:
    sleep = (main.get("payload") or {}).get("sleep") or {}
    awake = None
    for row in (sleep.get("summary") or {}).get("stagesSummary") or []:
        if row.get("type") == "AWAKE":
            awake = int(row.get("count") or 0)
    return awake, len(sleep.get("shortAwakenings") or [])


# ---------------------------------------------------------------- workouts

def workout_rows(store: Store, sessions: list[dict], rhr: dict[str, float], rhr_fallback: float | None,
                 hr_max: float | None, zone_hrr: dict[str, float] | None = None) -> list[dict]:
    raw = []
    for s in sessions:
        a, b = epoch(s["start_utc"] or s["start"]), epoch(s["end_utc"] or s["end"])
        if a is None or b is None or b <= a:
            continue
        ex = (s.get("payload") or {}).get("exercise") or {}
        summ = ex.get("metricsSummary") or {}
        etype = s["details"].get("exercise_type") or ex.get("exerciseType")
        raw.append({"s": s, "start": a, "end": b, "type": etype, "method": (s["source"] or {}).get("recordingMethod"),
                    "summary": summ})
    rows = []
    for r in S.dedupe_sessions(raw):
        s, summ = r["s"], r["summary"]
        minutes = s.get("duration") or (r["end"] - r["start"]) / 60
        samples = store.samples("heart", "heart_rate", s["start"], s["end"]) if s["start"] and s["end"] else []
        rest = rhr.get(s["date"], rhr_fallback)
        pts = [(epoch(t), v) for t, v in samples]
        covered = (pts[-1][0] - pts[0][0]) / 60 if len(pts) > 1 else 0
        zones = {k: _secs(summ.get("heartRateZoneDurations", {}).get(k + "Time")) for k in ("light", "moderate", "vigorous", "peak")}
        if pts and covered >= 0.5 * minutes and hr_max:
            trimp, method = S.trimp_from_samples(pts, rest, hr_max), "HR samples"
        else:
            zm = {k: (v / 60 if v is not None else None) for k, v in zones.items()}
            trimp, method = S.trimp_from_zones(zm, zone_hrr), "zone minutes"
        zm5 = S.zone_minutes(pts, hr_max) if pts and covered >= 0.5 * minutes else None
        if zm5:
            zone, zone_method = S.dominant_zone(zm5), "HR samples"
        elif s.get("average_heart_rate") and hr_max:
            zone, zone_method = max(1, S.hr_zone(s["average_heart_rate"], hr_max)), "average HR"
        else:
            zone, zone_method = None, None
        dist_km = (s.get("distance") or 0) / 1e6 or None
        active_min = s.get("active_duration") or minutes
        rows.append({
            "date": s["date"], "start": s["start"], "end": s["end"], "type": r["type"] or "WORKOUT",
            "label": TYPE_LABEL.get(r["type"], (r["type"] or "Workout").replace("_", " ").title()),
            "minutes": round(minutes, 1), "avg_hr": s.get("average_heart_rate"),
            "max_hr": max((v for _, v in samples), default=None),
            "zones_min": {k: (round(v / 60) if v is not None else None) for k, v in zones.items()},
            "kcal": s.get("calories"), "azm": s.get("active_zone_minutes"),
            "trimp": None if trimp is None else round(trimp, 1), "strain": S.strain(trimp), "strain_method": method,
            "distance_km": round(dist_km, 2) if dist_km else None,
            "pace_s_per_km": round(active_min * 60 / dist_km) if dist_km and dist_km >= 0.2 else None,
            "steps": s.get("steps"), "method": r["method"], "epoch": (r["start"], r["end"]),
            "zone": zone, "zone_method": zone_method,
            "zone_minutes": None if not zm5 else {z: round(v) for z, v in zm5.items()},
        })
    return rows


def _secs(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(str(v).rstrip("s"))
    except ValueError:
        return None


# ---------------------------------------------------------------- the model

def build_model(store: Store | None, day: date, cfg: dict, days: int = 28, period: str = "day",
                now: datetime | None = None) -> dict:
    now = now or datetime.now(NY)
    today_partial = day == now.date()
    hist = max(days, 28)
    model: dict = {
        "date": iso(day), "now": now.isoformat(), "partial_day": today_partial, "formula_version": S.FORMULA_VERSION,
        "config": {k: cfg.get(k) for k in ("bedtime_goal", "bedtime_step", "wake_anchor", "steps_goal", "gym_goal")},
        "days": span(day, hist), "has_data": False,
    }
    if store is None:
        model["empty_reason"] = "no local database at {}".format(data_home() / "fitbit.sqlite3")
        return model

    d = iso(day)
    lo = iso(day - timedelta(days=LOAD_SPAN))
    sync_at, synced_through = store.last_sync()
    cov = store.coverage()
    model.update({
        "last_sync": to_ny(sync_at), "synced_through": synced_through, "coverage": cov,
        "category_status": store.category_status(), "devices": store.devices(iso(day - timedelta(days=30)), d),
        "metrics": store.metrics(), "data_since": store.first_date(("steps", "minutes_asleep", "resting_heart_rate")),
        "baseline_window": [iso(day - timedelta(days=BASELINE_DAYS)), iso(day - timedelta(days=1))],
        "has_data": bool(cov),
    })
    if not cov:
        model["empty_reason"] = "the local database has no records yet"
        return model

    # ---- daily series
    hrv = store.daily("heart", "hrv_average", lo, d)
    rhr = store.daily("heart", "resting_heart_rate", lo, d)
    resp = store.daily("spo2_breathing_temperature", "breathing_rate", lo, d)
    spo2 = store.daily("spo2_breathing_temperature", "spo2_avg", lo, d)
    skin = store.daily("spo2_breathing_temperature", "skin_temperature_nightly", lo, d)
    skin_base = store.daily("spo2_breathing_temperature", "skin_temperature_baseline", lo, d)
    skin_sd = store.daily("spo2_breathing_temperature", "skin_temperature_relative_stddev_30d", lo, d)
    vo2 = store.daily("cardio_fitness", "vo2max", lo, d)
    vo2_level = store.daily_text("cardio_fitness", "cardio_fitness_level", lo, d)
    steps = store.daily("activity", "steps", lo, d)
    dist = store.daily("activity", "distance", lo, d)
    floors = store.daily("activity", "floors", lo, d)
    cal_out = store.daily("activity", "calories_out", lo, d)
    active_kcal = store.daily("activity", "active_energy", lo, d)
    act_mod = store.daily("activity", "active_minutes_moderate", lo, d)
    act_vig = store.daily("activity", "active_minutes_vigorous", lo, d)
    act_light = store.daily("activity", "active_minutes_light", lo, d)
    zone_light = store.daily("activity", "hr_zone_light_minutes", lo, d)
    sedentary = store.interval_sum("activity", "sedentary_minutes", lo, d)
    azm = store.azm_by_day(lo, d)
    hr_cov = store.hr_minutes_by_day(lo, d)
    hr_max = store.hr_max_estimate(iso(day - timedelta(days=180)), d)
    rhr_base_all = S.baseline(rhr.get(x) for x in span(day - timedelta(days=1), BASELINE_DAYS))
    model["hr_max_est"] = hr_max
    zone_hrr = S.zone_hrr_from_thresholds(store.zone_thresholds(d), rhr_base_all.mean if rhr_base_all else None, hr_max)
    model["zone_hrr"] = zone_hrr
    skin_dev = {x: skin[x] - skin_base[x] for x in skin if x in skin_base}

    def has_activity(x):
        return x in steps or x in azm

    # ---- wear coverage: minutes with an activity level + minutes asleep. Under 10 h means the band
    # probably wasn't worn, so the day is missing data, not a rest day (today, still in progress, is exempt).
    worn = store.minutes_by_day("activity", "activity_level_minutes", lo, d)
    worn_since = min(worn) if worn else None
    sleeps = store.sessions("sleep", lo, d)
    asleep_any: dict[str, float] = defaultdict(float)
    for s_ in sleeps:
        asleep_any[s_["date"]] += s_.get("minutes_asleep") or 0
    low_wear = {x for x in span(day, LOAD_SPAN) if worn_since and x >= worn_since and x != d
                and worn.get(x, 0) + asleep_any.get(x, 0) < WEAR_MIN_MINUTES}
    model["low_wear_days"] = sorted(y for y in low_wear if y >= iso(day - timedelta(days=hist)))

    # ---- strain per day: TRIMP from all-day HR when it covers 18 h, else from Active Zone Minutes
    trimp, strain_by_day, strain_method = {}, {}, {}
    for x in span(day, LOAD_SPAN):
        if x in low_wear:
            continue
        if hr_cov.get(x, 0) >= 18 * 60 and hr_max:
            pts = [(epoch(t), v) for t, v in store.samples("heart", "heart_rate", x + "T00:00:00", x + "T23:59:59")]
            t = S.trimp_from_samples(pts, rhr.get(x, rhr_base_all.mean if rhr_base_all else None), hr_max)
            method = "all-day HR"
        elif has_activity(x):
            z = azm.get(x, {})
            t = S.trimp_from_zones({k: z.get(k, 0) for k in ("FAT_BURN", "CARDIO", "PEAK")}, zone_hrr)
            method = "zone minutes"
        else:
            continue
        if t is not None:
            trimp[x], strain_by_day[x], strain_method[x] = t, S.strain(t), method

    # ---- sleep per night
    nights = nights_by_date(sleeps)
    asleep = {x: n["main"]["minutes_asleep"] for x, n in nights.items() if n["main"] and n["main"].get("minutes_asleep") is not None}
    nap_min = {x: sum(s.get("minutes_asleep") or 0 for s in n["naps"]) for x, n in nights.items()}
    need, perf = {}, {}
    for x in span(day, hist + BASELINE_DAYS):
        if x not in asleep:
            continue
        xd = date.fromisoformat(x)
        base_need, _ = S.baseline_sleep_need(asleep.get(y) for y in span(xd - timedelta(days=1), BASELINE_DAYS))
        prev = [asleep.get(iso(xd - timedelta(days=i))) for i in (1, 2, 3)]
        debt = S.sleep_debt(prev, base_need)
        yday = iso(xd - timedelta(days=1))
        need[x] = S.sleep_need(base_need, strain_by_day.get(yday), debt, nap_min.get(yday, 0))
        perf[x] = S.sleep_performance(asleep[x], need[x].total)

    # ---- sleep score per night (WHOOP-style): hours vs need, efficiency, 4-night consistency, stress
    import bisect
    hrv_samples = store.samples("heart", "hrv_rmssd", lo + "T00:00:00", d + "T23:59:59")
    hrv_t = [t for t, _ in hrv_samples]

    def night_hrv(x: str) -> list[float]:
        main_ = (nights.get(x) or {}).get("main")
        if not main_ or not main_.get("start") or not main_.get("end"):
            return []
        a_, b_ = bisect.bisect_left(hrv_t, main_["start"][:19]), bisect.bisect_right(hrv_t, main_["end"][:19])
        return [v for _, v in hrv_samples[a_:b_]]

    def onset_wake(x: str):
        main_ = (nights.get(x) or {}).get("main")
        return (mins_of_day(main_["start"]), mins_of_day(main_["end"])) if main_ else (None, None)

    sleep_scores: dict[str, S.SleepScore] = {}
    stress_pct: dict[str, float | None] = {}
    for x in perf:
        xd = date.fromisoformat(x)
        main_ = nights[x]["main"]
        in_bed = main_.get("time_in_sleep_period") or ((epoch(main_["end"]) - epoch(main_["start"])) / 60)
        eff = S.efficiency(main_.get("minutes_asleep"), in_bed)
        last4 = [onset_wake(iso(xd - timedelta(days=i))) for i in range(S.SLEEP_CONSISTENCY_NIGHTS - 1, -1, -1)]
        cons = S.consistency([o for o, _ in last4], [w_ for _, w_ in last4])
        base_vals = [v for y in span(xd - timedelta(days=1), BASELINE_DAYS) for v in night_hrv(y)]
        stress_pct[x] = S.sleep_stress_pct(night_hrv(x), base_vals)
        sleep_scores[x] = S.sleep_score(perf[x], eff, cons, stress_pct[x])
    sscore = {x: v.score for x, v in sleep_scores.items() if v.score is not None}

    # ---- recovery per day
    rec = {}
    for x in span(day, max(hist, LOAD_SPAN - BASELINE_DAYS)):
        xd = date.fromisoformat(x)
        win = span(xd - timedelta(days=1), BASELINE_DAYS)
        rec[x] = S.recovery(hrv.get(x), S.baseline(hrv.get(y) for y in win), rhr.get(x),
                            S.baseline(rhr.get(y) for y in win), sscore.get(x), resp.get(x),
                            S.baseline(resp.get(y) for y in win), skin_dev.get(x), skin_sd.get(x))

    days_list = model["days"]
    model["series"] = {
        "hrv": [hrv.get(x) for x in days_list], "rhr": [rhr.get(x) for x in days_list],
        "resp": [resp.get(x) for x in days_list], "spo2": [spo2.get(x) for x in days_list],
        "skin_dev": [None if x not in skin_dev else round(skin_dev[x], 2) for x in days_list],
        "vo2": [vo2.get(x) for x in days_list],
        "steps": [steps.get(x) for x in days_list],
        "strain": [strain_by_day.get(x) for x in days_list],
        "recovery": [rec[x].score for x in days_list],
        "sleep_perf": [perf.get(x) for x in days_list],
        "sleep_score": [sscore.get(x) for x in days_list],
        "asleep": [asleep.get(x) for x in days_list],
    }
    win28 = span(day - timedelta(days=1), BASELINE_DAYS)
    model["baselines"] = {k: _base_dict(S.baseline(src.get(y) for y in win28)) for k, src in
                          (("hrv", hrv), ("rhr", rhr), ("resp", resp), ("spo2", spo2), ("steps", steps),
                           ("strain", strain_by_day), ("asleep", asleep), ("vo2", vo2), ("skin_dev", skin_dev))}
    model["baselines"]["recovery"] = _base_dict(S.baseline(rec[y].score for y in win28 if y in rec))
    model["baselines"]["sleep_perf"] = _base_dict(S.baseline(perf.get(y) for y in win28))
    model["baselines"]["sleep_score"] = _base_dict(S.baseline(sscore.get(y) for y in win28))

    # ---- today: recovery, strain, sleep performance
    r = rec[d]
    model["recovery"] = {"score": r.score, "band": r.band, "reason": r.reason,
                         "contributions": [asdict(c) for c in r.contributions]}
    model["strain"] = {"day": strain_by_day.get(d), "trimp": None if d not in trimp else round(trimp[d], 1),
                       "method": strain_method.get(d), "target": S.strain_target(r.score),
                       "reason": None if d in strain_by_day else "no activity data today"}

    # ---- sleep detail
    n = nights.get(d)
    main = n["main"] if n else None
    sleep_model: dict = {"has_night": main is not None}
    if main:
        stg = {k: main.get("stage_{}_minutes".format(k)) for k in ("deep", "light", "rem", "awake")}
        in_bed = main.get("time_in_sleep_period") or ((epoch(main["end"]) - epoch(main["start"])) / 60)
        aw, short = awake_counts(main)
        nd = need.get(d)
        restorative = (stg["deep"] or 0) + (stg["rem"] or 0)
        recent = span(day, 7)
        sleep_model.update({
            "start": main["start"], "end": main["end"], "asleep": main.get("minutes_asleep"), "in_bed": round(in_bed),
            "stages": stg, "timeline": stage_timeline(main), "awake_count": aw, "short_awakenings": short,
            "efficiency": S.efficiency(main.get("minutes_asleep"), in_bed),
            "need": asdict(nd) if nd else None, "performance": perf.get(d),
            "score": asdict(sleep_scores[d]) if d in sleep_scores else None,
            "stress_pct": stress_pct.get(d),
            "restorative_min": restorative,
            "restorative_pct": round(100 * restorative / main["minutes_asleep"]) if main.get("minutes_asleep") else None,
            "consistency": (sleep_scores[d].components.get("consistency") or {}).get("score") if d in sleep_scores else None,
        })
    # going into tonight: last night counts toward debt, today's strain (so far) and naps adjust the need
    base_tonight, _ = S.baseline_sleep_need(asleep.get(y) for y in span(day, BASELINE_DAYS))
    debt_tonight = S.sleep_debt([asleep.get(iso(day - timedelta(days=i))) for i in (0, 1, 2)], base_tonight)
    need_tonight = S.sleep_need(base_tonight, strain_by_day.get(d), debt_tonight, nap_min.get(d, 0))
    wake = hhmm(cfg.get("wake_anchor")) if cfg.get("wake_anchor") else None
    sleep_model["tonight"] = {"need": asdict(need_tonight), "debt": round(debt_tonight),
                              "asleep_by": None if wake is None else S.asleep_by(wake, need_tonight.total),
                              "wake": wake, "strain_partial": today_partial}
    sleep_model["naps"] = [{"start": s["start"], "end": s["end"], "asleep": s.get("minutes_asleep")}
                           for s in (n["naps"] if n else [])]
    sleep_model["timing"] = [{"date": x, "start": nights[x]["main"]["start"], "end": nights[x]["main"]["end"],
                              "asleep": nights[x]["main"].get("minutes_asleep")} if nights.get(x, {}).get("main")
                             else {"date": x, "start": None, "end": None, "asleep": None} for x in span(day, 14)]
    model["sleep"] = sleep_model

    # ---- heart today
    samples_today = store.samples("heart", "heart_rate", d + "T00:00:00", d + "T23:59:59")
    z = azm.get(d, {})
    model["heart"] = {
        "hr_today": [(mins_of_day(t), v) for t, v in samples_today],
        "hr_rest": rhr.get(d, rhr_base_all.mean if rhr_base_all else None),
        "zones_today": {"light": zone_light.get(d), "fat_burn": z.get("FAT_BURN"), "cardio": z.get("CARDIO"),
                        "peak": z.get("PEAK")} if (d in zone_light or z) else None,
        "vo2_level": vo2_level.get(d) or (vo2_level[max(vo2_level)] if vo2_level else None),
        "spo2": spo2.get(d), "resp": resp.get(d), "skin_dev": skin_dev.get(d), "skin_sd": skin_sd.get(d),
        "stress": _stress_by_hour(samples_today, rhr.get(d), hr_max, hr_cov.get(d, 0)),
    }

    # ---- activity today
    def avg28(src):
        return mean(src.get(y) for y in win28)
    act_min = (act_mod.get(d) or 0) + (act_vig.get(d) or 0) if (d in act_mod or d in act_vig) else None
    model["activity"] = {
        "steps": steps.get(d), "steps_avg": avg28(steps),
        "distance_km": None if d not in dist else round(dist[d] / 1e6, 2),
        "distance_avg": None if avg28(dist) is None else round(avg28(dist) / 1e6, 2),
        "floors": floors.get(d), "floors_avg": avg28(floors),
        "cal_out": cal_out.get(d), "cal_out_avg": avg28(cal_out),
        "active_kcal": active_kcal.get(d), "basal_kcal": (cal_out[d] - active_kcal[d]) if d in cal_out and d in active_kcal else None,
        "azm": z.get("_points") if z else (0 if d in steps else None),
        "azm_avg": mean(azm.get(y, {}).get("_points", 0) if has_activity(y) else None for y in win28),
        "active_min": act_min,
        "active_min_avg": mean(((act_mod.get(y) or 0) + (act_vig.get(y) or 0)) if (y in act_mod or y in act_vig) else None for y in win28),
        "light_min": act_light.get(d),
        "sedentary_min": sedentary.get(d), "sedentary_avg": avg28(sedentary),
        "hourly": _hourly_activity(store.activity_levels(d)),
        "steps14": [steps.get(x) for x in span(day, 14)], "days14": span(day, 14),
    }
    goal = cfg.get("steps_goal") or 10000
    streak_days = span(day, 120)
    workouts_all = workout_rows(store, store.sessions("exercise", lo, d), rhr,
                                rhr_base_all.mean if rhr_base_all else None, hr_max, zone_hrr)
    workout_days = {w["date"] for w in workouts_all}
    first_steps = min(steps) if steps else d
    model["activity"]["streaks"] = {
        "steps_goal": asdict(S.streak([None if x not in steps else steps[x] >= goal for x in streak_days], today_partial)),
        "workout": asdict(S.streak([None if x < first_steps else x in workout_days for x in streak_days], today_partial)),
    }

    _label_lifts(workouts_all, cfg)
    lift_sets = cfg.get("lift_sets") or {}
    model["muscles"] = _muscles(workouts_all, now, day, model["recovery"]["score"], lift_sets)
    model["lifting"] = _lifting(lift_sets, day)
    journal = cfg.get("journal") or {}
    habit_list = cfg.get("habit_list") or J.DEFAULT_HABITS
    model["insights"] = _insights(day, nights, asleep, strain_by_day, steps, rec, workouts_all, low_wear,
                                  journal, habit_list)
    model["journal"] = _journal(journal, habit_list, day, model["insights"])
    model["hr_zones_bpm"] = None if not hr_max else [round(p * hr_max) for p in S.ZONE_PCT]

    # ---- workouts
    week = span(day, 7)
    model["workouts"] = {
        "week": [{k: v for k, v in w.items() if k != "epoch"} for w in workouts_all if w["date"] in week],
        "strip": [{"date": x, "types": [w["type"] for w in workouts_all if w["date"] == x],
                   "minutes": round(S.union_minutes(w["epoch"] for w in workouts_all if w["date"] == x))} for x in week],
        "records": _records(workouts_all, strain_by_day),
    }

    # ---- training plan
    monday = day - timedelta(days=day.weekday())
    gym = [w for w in workouts_all if w["type"] == "STRENGTH_TRAINING" and w["date"] >= iso(monday)]
    loads = [trimp.get(x) for x in span(day, 28)]
    impact = set(cfg.get("impact_types") or [])
    weeks = []
    for i in range(7, -1, -1):
        ws = monday - timedelta(days=7 * i)
        we = ws + timedelta(days=6)
        wk = [w for w in workouts_all if iso(ws) <= w["date"] <= iso(we) and w["type"] in impact]
        known = any(iso(ws) <= x <= iso(we) for x in steps)
        weeks.append({"start": iso(ws), "minutes": round(S.union_minutes(w["epoch"] for w in wk)) if known else None,
                      "partial": we >= day})
    spikes = S.impact_spikes([w["minutes"] for w in weeks])
    limit = S.impact_limit([w["minutes"] for w in weeks])
    for w, sp in zip(weeks, spikes):
        w["spike"] = sp
    split_info = _split_next(cfg, workouts_all)
    split_info["fresh"] = {d_: split_freshness(model["muscles"], d_) for d_ in split_info.get("queue") or []}
    model["training"] = {
        "gym_week": len(gym), "gym_goal": cfg.get("gym_goal") or 4,
        "gym_dates": [w["date"] for w in gym],
        "split": split_info,
        "acwr": asdict(S.acwr(loads)),
        "impact_weeks": weeks,
        "impact_limit": limit,
        "impact_spike_recent": any(spikes[-2:]),
    }

    # ---- body & nutrition
    w_pts = store.weights(lo, d)
    height = store.latest_value("body", "height")
    trend = S.weight_trend([((date.fromisoformat(x) - day).days, kg) for x, kg in w_pts])
    latest_kg = w_pts[-1][1] if w_pts else None
    height_m = (height[1] / 1000) if height and (height[2] or "mm") == "mm" else None
    weekly = defaultdict(list)
    for x, kg in w_pts:
        xd = date.fromisoformat(x)
        weekly[iso(xd - timedelta(days=xd.weekday()))].append(kg)
    cal_in = store.interval_sum("nutrition", "calories_in", lo, d)
    macros = {m: store.interval_sum("nutrition", m, d, d).get(d) for m in ("protein", "carbohydrate", "fat")}
    water = store.interval_sum("nutrition", "water", d, d).get(d)
    stale_weight = (day - date.fromisoformat(w_pts[-1][0])).days if w_pts else None
    model["body"] = {
        "weights": w_pts, "days_since_weigh_in": stale_weight,
        "pace_reliable": len(w_pts) >= 3 and stale_weight is not None and stale_weight <= 14, "weekly_avg": {k: round(statistics.fmean(v), 2) for k, v in sorted(weekly.items())},
        "trend": asdict(trend), "latest_kg": latest_kg,
        "bmi": round(latest_kg / height_m ** 2, 1) if latest_kg and height_m else None,
        "pace_target_kg": [round(latest_kg * p / 100, 2) for p in S.BULK_PACE] if latest_kg else None,
        "body_fat": store.latest_value("body", "body_fat"),
        "cal_in_today": cal_in.get(d), "cal_out_today": cal_out.get(d), "macros": macros, "water_ml": water,
        "last_food_log": max(cal_in) if cal_in else None,
        "cal_in_14": [cal_in.get(x) for x in span(day, 14)], "cal_out_14": [cal_out.get(x) for x in span(day, 14)],
    }

    # ---- logs
    st = {row["grp"]: row for row in model["category_status"]}
    model["logs"] = {k: (st.get(g) or {}).get("status", "not requested") for k, g in
                     (("symptoms", "symptoms"), ("mood", "moods"), ("mindfulness", "mindfulness"))}

    # ---- experiment
    model["experiment"] = _experiment(cfg.get("experiment"), nights, day)

    # ---- verdict
    model["verdict"] = _verdict(model)

    # ---- coach note: the newest written note for today, else a rule-based one from the same numbers
    note = CO.pick(cfg.get("coach_notes") or [], d, now) if today_partial else \
        CO.pick(cfg.get("coach_notes") or [], d, datetime.combine(day, datetime.max.time(), NY))
    model["coach"] = {"note": note,
                      "fallback": None if note else CO.fallback(model, now if today_partial else
                                                               datetime.combine(day, datetime.max.time(), NY))}

    # ---- period view
    metrics = {
        "Recovery %": ({x: rec[x].score for x in rec if rec[x].score is not None}, True),
        "Sleep score": (sscore, True), "Sleep perf %": (perf, True), "Asleep h": ({x: v / 60 for x, v in asleep.items()}, True),
        "Strain": (strain_by_day, None), "HRV ms": (hrv, True), "Resting HR": (rhr, False),
        "Resp. rate": (resp, False), "Steps": (steps, True), "Active kcal": (active_kcal, True),
        "AZM": ({x: azm.get(x, {}).get("_points", 0) for x in steps}, True),
        "Workout min": ({x: S.union_minutes(w["epoch"] for w in workouts_all if w["date"] == x) for x in steps}, None),
        "Lifting min": ({x: sum(w["minutes"] for w in workouts_all if w["date"] == x and w["type"] == "STRENGTH_TRAINING")
                         for x in steps}, True),
        "Weight kg": (dict(w_pts), None),
    }
    additive = {"Steps", "Active kcal", "AZM", "Workout min", "Lifting min", "Strain"}
    # always: the last 7 days vs the 7 before, for the dashboard's "This week vs last" box
    model["week_review"] = _period("week", day, today_partial, metrics, additive=additive)
    if period in ("week", "month"):
        model["period"] = _period(period, day, today_partial, metrics, additive=additive)
    return model


def _label_lifts(workouts: list[dict], cfg: dict) -> None:
    """Give each strength session a split day. `lift_log` entries (date → day) are what the user told
    us; from the split anchor on, the rest follow the rotation ("assumed"). Earlier ones stay unknown."""
    split = cfg.get("split") or []
    anchor = cfg.get("split_anchor") or {}
    log = cfg.get("lift_log") or {}
    lifts = sorted((w for w in workouts if w["type"] == "STRENGTH_TRAINING"), key=lambda w: (w["date"], w["start"]))
    idx = None
    for w in lifts:
        w["split"], w["split_source"] = None, None
        if w["date"] in log and log[w["date"]] in split:
            w["split"], w["split_source"] = log[w["date"]], "logged"
            idx = split.index(log[w["date"]])
        elif anchor.get("date") and anchor.get("day") in split and w["date"] >= anchor["date"]:
            if idx is None:
                idx = split.index(anchor["day"])
                w["split"], w["split_source"] = anchor["day"], "logged" if w["date"] == anchor["date"] else "assumed"
            else:
                idx = (idx + 1) % len(split)
                w["split"], w["split_source"] = split[idx], "assumed"
        w["muscles"] = S.split_muscles(w["split"]) if w["split"] else []


def _muscles(workouts: list[dict], now: datetime, day: date, recovery_score: float | None,
             lift_sets: dict[str, list[dict]] | None = None) -> dict:
    """Freshness per muscle from the fatigue model (scores.muscle_freshness). On days with logged
    sets (`fitdash lift`) the real sets decide which muscles were hit and how hard; otherwise the
    split day and the session's minutes and strain do."""
    lift_sets = lift_sets or {}
    ref = now if day == now.date() else datetime.combine(day, datetime.max.time(), NY)
    since7 = iso(day - timedelta(days=7))
    sessions, used = [], []
    unlabeled = []

    def add(end: datetime, dose: float, targets: dict, row: dict) -> None:
        sessions.append({"hours": (ref - end).total_seconds() / 3600, "dose": dose, "targets": targets})
        used.append({**row, "dose": round(dose), "targets": sorted(targets)})

    logged_days = {x for x in lift_sets if since7 <= x <= iso(day)}
    for w in sorted(workouts, key=lambda w: (w["date"], w["start"])):
        if w["date"] < since7 or w["date"] > iso(day):
            continue
        if w["type"] == "STRENGTH_TRAINING":
            if w["date"] in logged_days:
                continue                       # counted once below, from the real sets
            targets = S.split_targets(w.get("split"))
            if not targets:
                unlabeled.append(w["date"])
                continue
            what, source = w["split"], w.get("split_source")
        else:
            targets = S.activity_targets(w["type"])
            if not targets:
                continue
            what, source = w["label"], "activity"
        end = datetime.fromisoformat(w["end"][:19]).replace(tzinfo=NY)
        add(end, S.session_dose(w["minutes"], w["strain"]), targets, {"date": w["date"], "what": what, "source": source})
    for x in sorted(logged_days):
        lifts_ = [w for w in workouts if w["date"] == x and w["type"] == "STRENGTH_TRAINING"]
        if lifts_:
            end = max(datetime.fromisoformat(w["end"][:19]) for w in lifts_).replace(tzinfo=NY)
        else:
            end = max(datetime.fromisoformat(e.get("at") or x + "T18:00") for e in lift_sets[x]).replace(tzinfo=NY)
        dose = S.lift_dose(lift_sets[x])
        if not dose:
            continue
        top = sorted(dose, key=lambda mu: -dose[mu])
        add(end, 100.0, {mu: v / 100.0 for mu, v in dose.items()},
            {"date": x, "what": "logged: " + ", ".join(top[:3]), "source": "sets"})
    used.sort(key=lambda u: u["date"])
    rate = S.recovery_rate(recovery_score)
    fresh = S.muscle_freshness(sessions, rate)
    last: dict[str, dict] = {}
    for u in used:
        for mu in u["targets"]:
            last[mu] = u
    rows = [{"muscle": mu, "fresh": fresh[mu], "last": (last.get(mu) or {}).get("date"),
             "split": (last.get(mu) or {}).get("what"), "source": (last.get(mu) or {}).get("source")}
            for mu in S.MUSCLES]
    has_strength = any(w["type"] == "STRENGTH_TRAINING" and w["date"] >= iso(day - timedelta(days=30))
                       for w in workouts) or any(x >= iso(day - timedelta(days=30)) for x in lift_sets)
    unlabeled = [x for x in unlabeled if x not in logged_days]
    return {"rows": rows, "sessions": used, "rate": rate, "has_strength": has_strength,
            "unlabeled_recent": [d_ for d_ in unlabeled if d_ >= iso(day - timedelta(days=4))],
            "unlabeled": unlabeled}


def _lifting(lift_sets: dict[str, list[dict]], day: date) -> dict:
    """Weekly hard sets per muscle (last 7 days) vs the hypertrophy range, and per-lift progress as
    the best estimated 1-rep max of each session over the last 8 weeks."""
    d, wk0 = iso(day), iso(day - timedelta(days=6))
    prev0, prev1 = iso(day - timedelta(days=13)), iso(day - timedelta(days=7))
    week = [e for x, es in lift_sets.items() if wk0 <= x <= d for e in es]
    prev = [e for x, es in lift_sets.items() if prev0 <= x <= prev1 for e in es]
    sets, sets_prev = S.weekly_sets(week), S.weekly_sets(prev)
    since = iso(day - timedelta(days=56))
    prog: dict[str, list[dict]] = defaultdict(list)
    for x in sorted(lift_sets):
        if not since <= x <= d:
            continue
        best: dict[str, tuple[float, dict]] = {}
        for e in lift_sets[x]:
            v = S.e1rm(e.get("kg"), e["reps"])
            if v is not None and (e["exercise"] not in best or v > best[e["exercise"]][0]):
                best[e["exercise"]] = (v, e)
        for ex, (v, e) in best.items():
            prog[ex].append({"date": x, "e1rm": round(v, 1), "top": "{}x{}@{:g}".format(e["sets"], e["reps"], e["kg"])})
    lifts_ = []
    for ex, pts in prog.items():
        first, last_ = pts[0]["e1rm"], pts[-1]["e1rm"]
        lifts_.append({"exercise": ex, "points": pts, "last": pts[-1], "best": max(p["e1rm"] for p in pts),
                       "change_pct": None if len(pts) < 2 else round(100 * (last_ - first) / first, 1),
                       "pr": len(pts) >= 2 and last_ >= max(p["e1rm"] for p in pts[:-1]) + 0.05})
    lifts_.sort(key=lambda r: r["last"]["date"], reverse=True)
    lo, hi = S.HYPERTROPHY_SETS
    return {
        "has_log": bool(lift_sets), "logged_days_week": sorted({x for x in lift_sets if wk0 <= x <= d}),
        "sets": {mu: round(v, 1) for mu, v in sets.items()},
        "sets_prev": {mu: round(v, 1) for mu, v in sets_prev.items()},
        "target": [lo, hi],
        "under": [mu for mu, v in sets.items() if v < lo], "over": [mu for mu, v in sets.items() if v > hi],
        "lifts": lifts_, "last_logged": max(lift_sets) if lift_sets else None,
    }


INSIGHT_MIN_DAYS = 21


def _insights(day: date, nights: dict, asleep: dict, strain_by_day: dict, steps: dict, rec: dict,
              workouts: list[dict], low_wear: set, journal: dict | None = None,
              habit_list: list[dict] | None = None) -> dict:
    """What tends to come before your better and worse recovery mornings, in your own history.

    Each factor splits the mornings into two groups by something you did the night or day before and
    compares average Recovery (scores.compare, Welch's t). Correlation in a small personal sample,
    not proof: a factor is shown only with ≥ 5 mornings on each side."""
    days_ = [x for x in sorted(rec) if rec[x].score is not None and x <= iso(day)]
    out: dict = {"n_days": len(days_), "since": days_[0] if days_ else None, "effects": [],
                 "min_days": INSIGHT_MIN_DAYS}
    if len(days_) < INSIGHT_MIN_DAYS:
        return out

    def prev(x: str) -> str:
        return iso(date.fromisoformat(x) - timedelta(days=1))

    def onset(x: str) -> float | None:
        main_ = (nights.get(x) or {}).get("main")
        m_ = mins_of_day(main_["start"]) if main_ else None
        return None if m_ is None else m_ - 1440 if m_ >= 720 else m_     # minutes after midnight, negative before

    onsets = [o for o in (onset(x) for x in days_) if o is not None]
    med_onset = statistics.median(onsets) if onsets else None
    strains = [strain_by_day[prev(x)] for x in days_ if prev(x) in strain_by_day]
    hard = max(12.0, round(S.percentile(strains, 0.75) or 12.0)) if strains else 12.0
    late_end = {w["date"] for w in workouts if mins_of_day(w["end"]) is not None and mins_of_day(w["end"]) >= 20 * 60}
    lifted = {w["date"] for w in workouts if w["type"] == "STRENGTH_TRAINING"}

    def factor(test, yes_label: str, no_label: str, key: str, source: str = "data") -> None:
        yes, no = [], []
        for x in days_:
            v = test(x)
            if v is None:
                continue
            (yes if v else no).append(rec[x].score)
        eff = S.compare(yes, no, yes_label)
        if eff:
            out["effects"].append({**asdict(eff), "key": key, "no_label": no_label, "source": source,
                                   "mean_yes": round(statistics.fmean(yes)), "mean_no": round(statistics.fmean(no))})

    factor(lambda x: None if onset(x) is None else onset(x) <= 60, "asleep by 1:00", "after 1:00", "bedtime")
    factor(lambda x: None if x not in asleep else asleep[x] >= 420, "7 h+ asleep", "under 7 h", "duration")
    if med_onset is not None:
        factor(lambda x: None if onset(x) is None else abs(onset(x) - med_onset) <= 45,
               "asleep within 45 min of usual " + _clock(med_onset), "off by more", "regularity")
    factor(lambda x: None if prev(x) not in strain_by_day else strain_by_day[prev(x)] >= hard,
           "strain {:g}+ the day before".format(hard), "lighter day", "strain")
    factor(lambda x: None if prev(x) in low_wear else prev(x) in late_end,
           "workout past 20:00 the day before", "no late workout", "late_workout")
    factor(lambda x: None if prev(x) in low_wear else prev(x) in lifted, "lifted the day before", "didn't lift", "lifted")
    factor(lambda x: None if prev(x) not in steps or prev(x) in low_wear else steps[prev(x)] >= 10000,
           "10k+ steps the day before", "fewer steps", "steps")
    # journal habits: an entry is about the day it happened, so it's compared with the next morning
    for h in habit_list or []:
        factor(lambda x, k=h["key"]: ((journal or {}).get(prev(x)) or {}).get("habits", {}).get(k),
               h["label"] + " the day before", "not", "habit:" + h["key"], "journal")
    rank = {"clear": 0, "likely": 1, "unclear": 2}
    out["effects"].sort(key=lambda e: (rank[e["strength"]], -abs(e["t"])))
    return out


def _journal(journal: dict, habit_list: list[dict], day: date, insights: dict) -> dict:
    """The last 14 days of habits as a grid, 7-day counts, the logging streak and the latest note."""
    days14 = span(day, 14)
    effects = {e["key"][6:]: e for e in insights.get("effects", []) if e["key"].startswith("habit:")}
    rows = []
    for h in habit_list:
        cells = [((journal.get(x) or {}).get("habits") or {}).get(h["key"]) for x in days14]
        last7 = cells[-7:]
        rows.append({**h, "cells": cells, "yes7": sum(1 for c in last7 if c is True),
                     "logged7": sum(1 for c in last7 if c is not None), "effect": effects.get(h["key"])})
    streak, x = 0, day if iso(day) in journal else day - timedelta(days=1)
    while iso(x) in journal:
        streak, x = streak + 1, x - timedelta(days=1)
    notes = [(x, journal[x]["note"]) for x in sorted(journal) if x <= iso(day) and journal[x].get("note")]
    return {"has_log": any(x <= iso(day) for x in journal), "days": days14, "rows": rows,
            "logged7": sum(1 for x in days14[-7:] if x in journal), "streak": streak,
            "today_logged": iso(day) in journal, "yesterday_logged": iso(day - timedelta(days=1)) in journal,
            "last_note": notes[-1] if notes else None, "entries": len(journal)}


def _clock(minutes_rel: float) -> str:
    m_ = int(round(minutes_rel)) % 1440
    return "{}:{:02d}".format(m_ // 60, m_ % 60)


def split_freshness(muscles: dict, day_name: str | None) -> int | None:
    """How ready a split day is: its least-recovered muscle."""
    if not day_name:
        return None
    by = {r["muscle"]: r["fresh"] for r in muscles["rows"]}
    primary = [mu for mu, share in S.split_targets(day_name).items() if share >= 1.0]
    vals = [by.get(mu) for mu in primary]
    return None if not vals or any(v is None for v in vals) else min(vals)


def _base_dict(b: S.Baseline | None) -> dict | None:
    return None if b is None else {"mean": round(b.mean, 2), "sd": round(b.sd, 2), "n": b.n}


def _hourly_activity(levels: list[tuple[str, str]]) -> list[int | None] | None:
    if not levels:
        return None
    hours: list[int | None] = [None] * 24
    for t, lvl in levels:
        dt = local_dt(t)
        if dt is None:
            continue
        hours[dt.hour] = (hours[dt.hour] or 0) + (0 if lvl in ("SEDENTARY", None) else 1)
    return hours


def _stress_by_hour(samples: list[tuple[str, float]], rest: float | None, hr_max: float | None,
                    covered_minutes: int) -> dict:
    if covered_minutes < 12 * 60:
        return {"hours": None, "reason": "needs all-day heart rate (only workout windows are imported)"}
    by_h: dict[int, list[float]] = defaultdict(list)
    for t, v in samples:
        dt = local_dt(t)
        if dt and 7 <= dt.hour < 23:
            by_h[dt.hour].append(v)
    return {"hours": [S.stress_level(by_h.get(h, []), rest, hr_max) for h in range(24)], "reason": None}


def _records(workouts: list[dict], strain_by_day: dict[str, float]) -> dict:
    runs = [w for w in workouts if w["type"] in ("RUNNING", "TRAIL_RUN", "TREADMILL_RUNNING") and w["distance_km"]]
    longest = max(runs, key=lambda w: w["distance_km"], default=None)
    paced = [w for w in runs if w["pace_s_per_km"] and w["distance_km"] >= 1.0]
    fastest = min(paced, key=lambda w: w["pace_s_per_km"], default=None)
    top = max(strain_by_day.items(), key=lambda kv: kv[1], default=None)
    return {
        "longest_run": None if not longest else {"date": longest["date"], "km": longest["distance_km"]},
        "fastest_pace": None if not fastest else {"date": fastest["date"], "s_per_km": fastest["pace_s_per_km"],
                                                  "km": fastest["distance_km"]},
        "highest_strain": None if not top else {"date": top[0], "strain": top[1]},
    }


def _split_next(cfg: dict, workouts: list[dict]) -> dict:
    split = cfg.get("split") or []
    anchor = cfg.get("split_anchor") or {}
    if not split:
        return {"next": None, "queue": [], "reason": "no split configured"}
    if not anchor.get("date") or anchor.get("day") not in split:
        return {"next": None, "queue": split,
                "reason": "set split_anchor in ~/.fitbit-mcp/coaching/stats.json (data can't tell muscle groups)"}
    done_after = [w for w in workouts if w["type"] == "STRENGTH_TRAINING" and w["date"] > anchor["date"]]
    idx = (split.index(anchor["day"]) + 1 + len(done_after)) % len(split)
    queue = split[idx:] + split[:idx]
    return {"next": split[idx], "queue": queue, "reason": None, "since_anchor": len(done_after),
            "last_done": queue[-1], "last_done_date": max((w["date"] for w in done_after), default=anchor["date"])}


def _experiment(exp: dict | None, nights: dict, day: date) -> dict | None:
    if not exp or not exp.get("start"):
        return None
    start = date.fromisoformat(exp["start"])
    lights_out = S.clock_offset(hhmm(exp.get("lights_out") or "00:00"))
    rows = []
    for i in range(int(exp.get("nights") or 14)):
        x = iso(start + timedelta(days=i))
        main = nights.get(x, {}).get("main")
        if date.fromisoformat(x) > day:
            rows.append({"date": x, "state": "upcoming"})
        elif not main:
            rows.append({"date": x, "state": "missing"})
        else:
            onset = S.clock_offset(mins_of_day(main["start"]))
            rows.append({"date": x, "state": "hit" if onset <= lights_out else "miss",
                         "start": main["start"], "asleep": main.get("minutes_asleep")})
    base_days = span(start - timedelta(days=1), BASELINE_DAYS)
    base_on = [S.clock_offset(mins_of_day(nights[x]["main"]["start"])) for x in base_days if nights.get(x, {}).get("main")]
    base_sl = [nights[x]["main"].get("minutes_asleep") for x in base_days if nights.get(x, {}).get("main")]
    done = [r for r in rows if r["state"] in ("hit", "miss")]
    return {"name": exp.get("name"), "start": exp["start"], "lights_out": exp.get("lights_out"), "nights": rows,
            "hits": sum(r["state"] == "hit" for r in rows), "logged": len(done),
            "baseline_onset": statistics.median(base_on) if base_on else None,
            "baseline_asleep": statistics.median([v for v in base_sl if v]) if base_sl else None,
            "exp_onset": statistics.median(S.clock_offset(mins_of_day(r["start"])) for r in done) if done else None,
            "exp_asleep": statistics.median(r["asleep"] for r in done if r.get("asleep")) if done else None}


def _verdict(m: dict) -> dict:
    reasons = []
    rec = m["recovery"]
    if rec["score"] is None:
        reasons.append(("none", "Recovery — ({})".format(rec["reason"])))
    else:
        lvl = {"green": "good", "yellow": "watch", "red": "flag"}[rec["band"]]
        top = sorted(rec["contributions"], key=lambda c: abs(c["points"]), reverse=True)[:2]
        why = ", ".join("{} {:+.0f}".format(c["label"], c["points"]) for c in top)
        reasons.append((lvl, "Recovery {}% ({})".format(rec["score"], why)))
    sl = m["sleep"]
    if sl.get("has_night") and (sl.get("score") or {}).get("score") is not None:
        sc = sl["score"]
        lvl = {"optimal": "good", "sufficient": "watch", "poor": "flag"}[sc["band"]]
        weakest = min(((k, c["score"]) for k, c in sc["components"].items() if c.get("score") is not None), key=lambda kv: kv[1])
        names = {"sufficiency": "hours vs need", "efficiency": "efficiency", "consistency": "consistency", "stress": "sleep stress"}
        reasons.append((lvl, "Sleep score {} ({}), {} asleep{}".format(
            sc["score"], sc["band"], _hm(sl["asleep"]),
            "; weakest: {} {}".format(names[weakest[0]], weakest[1]) if sc["band"] != "optimal" else "")))
    else:
        reasons.append(("none", "No sleep recorded for last night"))
    ac = m["training"]["acwr"]
    if ac["ratio"] is not None:
        lvl = "good" if ac["zone"] == "sweet spot" else "flag" if ac["zone"] == "high" else "watch"
        reasons.append((lvl, "Load ratio {:.2f} ({})".format(ac["ratio"], ac["zone"])))
    if m["training"]["impact_spike_recent"]:
        reasons.append(("watch", "Running/soccer spike this or last week (knee)"))
    h = m["heart"]
    if h["skin_dev"] is not None and h["skin_sd"] and abs(h["skin_dev"]) > 2 * h["skin_sd"]:
        reasons.append(("watch", "Skin temp {:+.1f} °C off baseline".format(h["skin_dev"])))
    band = rec["band"]
    target = m["strain"]["target"]
    knee = " · lifting or low-impact only, no runs or jumps" if m["training"]["impact_spike_recent"] else ""
    if band is None:
        headline, level = "Not enough data for a recovery score", "none"
    elif band == "green":
        headline, level = "Primed: go hard today (target strain {:.0f}–{:.0f}){}".format(*target, knee), "good"
    elif band == "yellow":
        headline, level = "Maintain: train moderately (target strain {:.0f}–{:.0f}){}".format(*target, knee), "watch"
    else:
        headline, level = "Recover: keep it light (target strain under {:.0f})".format(target[1]), "flag"
    return {"headline": headline, "level": level, "reasons": reasons}


def _hm(minutes) -> str:
    if minutes is None:
        return "—"
    h, mm = divmod(int(round(minutes)), 60)
    return "{}h{:02d}".format(h, mm) if h else "{}m".format(mm)


def _period(period: str, day: date, partial: bool, metrics: dict, additive: set) -> dict:
    n = 7 if period == "week" else 30
    cur = span(day, n)
    prev = span(day - timedelta(days=n), n)
    rows = []
    for name, (src, higher_better) in metrics.items():
        use = [x for x in cur if not (partial and x == cur[-1] and name in additive)]
        vals = [(x, src.get(x)) for x in use if src.get(x) is not None]
        pvals = [src.get(x) for x in prev if src.get(x) is not None]
        if not vals:
            rows.append({"metric": name, "avg": None, "prev": mean(pvals), "n": 0, "series": [src.get(x) for x in cur]})
            continue
        avg = statistics.fmean(v for _, v in vals)
        best = worst = None
        if higher_better is not None:
            ordered = sorted(vals, key=lambda kv: kv[1], reverse=higher_better)
            best, worst = ordered[0], ordered[-1]
        series = [src.get(x) for x in cur]
        if partial and name in additive:
            series[-1] = None          # an in-progress day isn't comparable; show it as a gap
        rows.append({"metric": name, "avg": avg, "prev": mean(pvals), "n": len(vals), "higher_better": higher_better,
                     "best": best, "worst": worst, "series": series})
    return {"period": period, "days": cur, "prev_days": [prev[0], prev[-1]], "rows": rows,
            "excluded_today": partial}
