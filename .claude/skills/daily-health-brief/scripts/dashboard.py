#!/usr/bin/env python3
"""Daily health dashboard for the terminal, read from the local fitbit-mcp database.

Read-only by construction: the SQLite file is opened with mode=ro and only fixed,
parameterized queries run. Nothing here talks to the network; sync first through the
fitbit-local MCP tools.

  python3 dashboard.py                 # color dashboard for today (dark terminal)
  python3 dashboard.py --theme light   # palette stepped for a light terminal
  python3 dashboard.py --plain         # no ANSI color, same charts (for pasting into chat)
  python3 dashboard.py --json          # numbers + readiness signals, for coaching
  python3 dashboard.py --date 2026-10-07 --days 21
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path(os.environ.get("FITBIT_MCP_HOME", "~/.fitbit-mcp")).expanduser()
WIDTH = 74
BASELINE_DAYS = 28

# Colors come from the dataviz reference palette. Sleep stages use categorical slots 1-4 in
# fixed order (validated adjacent-pair CVD-safe in both modes); each stage also has its own
# glyph, so identity never rests on color alone and --plain loses nothing.
PALETTES = {
    "dark": {"series": "#3987e5", "deep": "#3987e5", "light": "#d95926", "rem": "#199e70", "awake": "#c98500",
             "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781", "rule": "#383835"},
    "light": {"series": "#2a78d6", "deep": "#2a78d6", "light": "#eb6834", "rem": "#1baf7a", "awake": "#eda100",
              "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "rule": "#c3c2b7"},
}
STATUS = {"good": ("#0ca30c", "✓"), "watch": ("#fab219", "!"), "flag": ("#d03b3b", "✕"), "none": ("#898781", "·")}
STAGES = (("deep", "Deep", "█"), ("light", "Light", "▓"), ("rem", "REM", "▒"), ("awake", "Awake", "░"))
EIGHTHS = " ▁▂▃▄▅▆▇█"
SHORT = {"STRENGTH_TRAINING": "Strength", "CARDIO_WORKOUT": "Cardio", "WALKING": "Walk", "RUNNING": "Run",
         "TRAIL_RUN": "Trail run", "SOCCER": "Soccer", "BOXING": "Boxing", "WORKOUT": "Workout"}


# ---------------------------------------------------------------- data access (read-only)

def connect() -> sqlite3.Connection:
    path = HOME / "fitbit.sqlite3"
    if not path.exists():
        sys.exit("No local database at {}. Connect and import through the fitbit-local tools first.".format(path))
    conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def daily(conn, category: str, metric: str, start: str, end: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT local_date, value FROM records WHERE category=? AND metric=? AND granularity='daily' "
        "AND status='active' AND value IS NOT NULL AND local_date BETWEEN ? AND ? ORDER BY local_date, updated_at",
        (category, metric, start, end))
    return {r["local_date"]: r["value"] for r in rows}


def daily_text(conn, category: str, metric: str, day: str) -> str | None:
    row = conn.execute(
        "SELECT value_text FROM records WHERE category=? AND metric=? AND granularity='daily' AND status='active' "
        "AND local_date=? ORDER BY updated_at DESC LIMIT 1", (category, metric, day)).fetchone()
    return row["value_text"] if row else None


def sessions(conn, category: str, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT source_id, metric, value, local_date, start_local, end_local, details FROM records "
        "WHERE category=? AND granularity='session' AND status='active' AND local_date BETWEEN ? AND ? "
        "ORDER BY start_local", (category, start, end))
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r["source_id"], {"date": r["local_date"], "start": r["start_local"], "end": r["end_local"],
                                           "details": json.loads(r["details"] or "{}")})
        s[r["metric"]] = r["value"]
    return list(out.values())


def last_sync(conn) -> str | None:
    row = conn.execute("SELECT MAX(last_sync_at) AS t FROM sync_state").fetchone()
    return row["t"] if row else None


# ---------------------------------------------------------------- helpers

def days_back(day: date, n: int) -> list[str]:
    return [(day - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def hm(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    h, m = divmod(int(round(minutes)), 60)
    return "{}h {:02d}m".format(h, m) if h else "{}m".format(m)


def clock(ts: str | None) -> str:
    if not ts:
        return "—"
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is not None:  # sync timestamps are UTC; session times are already local
        t = t.astimezone()
    return t.strftime("%-I:%M %p")


def baseline(series: dict[str, float], day: date) -> dict | None:
    vals = [series[d] for d in days_back(day - timedelta(days=1), BASELINE_DAYS) if d in series]
    if len(vals) < 7:
        return None
    return {"mean": round(statistics.fmean(vals), 1), "sd": round(statistics.pstdev(vals), 1), "n": len(vals)}


def worst(*levels: str) -> str:
    order = ["good", "watch", "flag"]
    present = [lv for lv in levels if lv in order]
    return max(present, key=order.index) if present else "none"


# ---------------------------------------------------------------- digest (numbers + signals)

def build_digest(conn, day: date, history: int) -> dict:
    d = day.isoformat()
    span_start = (day - timedelta(days=max(history, BASELINE_DAYS) + 1)).isoformat()

    rhr = daily(conn, "heart", "resting_heart_rate", span_start, d)
    hrv = daily(conn, "heart", "hrv_average", span_start, d)
    steps = daily(conn, "activity", "steps", span_start, d)
    sleeps = sessions(conn, "sleep", span_start, d)
    workouts = sessions(conn, "exercise", span_start, d)

    asleep_by_night: dict[str, float] = {}
    for s in sleeps:
        if s.get("minutes_asleep") is not None:
            asleep_by_night[s["date"]] = asleep_by_night.get(s["date"], 0) + s["minutes_asleep"]
    tonight = [s for s in sleeps if s["date"] == d]
    main = next((s for s in tonight if s["details"].get("is_main_sleep")), None) or \
        max(tonight, key=lambda s: s.get("minutes_asleep") or 0, default=None)

    load_by_day: dict[str, float] = {}
    for w in workouts:
        load_by_day[w["date"]] = load_by_day.get(w["date"], 0) + (w.get("duration") or 0)

    skin = daily(conn, "spo2_breathing_temperature", "skin_temperature_nightly", d, d).get(d)
    skin_base = daily(conn, "spo2_breathing_temperature", "skin_temperature_baseline", d, d).get(d)
    skin_sd = daily(conn, "spo2_breathing_temperature", "skin_temperature_relative_stddev_30d", d, d).get(d)

    def act(metric):
        return daily(conn, "activity", metric, d, d).get(d)

    # ---- readiness signals: transparent thresholds, explained in SKILL.md
    signals = []
    asleep = asleep_by_night.get(d)
    if asleep is None:
        signals.append({"key": "sleep", "level": "none", "text": "No sleep recorded for last night"})
    else:
        lvl = "good" if asleep >= 420 else "watch" if asleep >= 360 else "flag"
        signals.append({"key": "sleep", "level": lvl, "text": "Slept {}".format(hm(asleep))})
    last3 = [asleep_by_night[x] for x in days_back(day, 3) if x in asleep_by_night]
    if len(last3) == 3 and statistics.fmean(last3) < 390:
        signals.append({"key": "sleep_debt", "level": "watch",
                        "text": "3-night average {}".format(hm(statistics.fmean(last3)))})

    rhr_b = baseline(rhr, day)
    if d in rhr and rhr_b:
        delta = rhr[d] - rhr_b["mean"]
        lvl = "good" if delta <= 2 else "watch" if delta < 5 else "flag"
        signals.append({"key": "rhr", "level": lvl,
                        "text": "Resting HR {:.0f} ({:+.1f} vs {}-day avg {})".format(rhr[d], delta, rhr_b["n"], rhr_b["mean"])})
    hrv_b = baseline(hrv, day)
    if d in hrv and hrv_b:
        pct = (hrv[d] - hrv_b["mean"]) / hrv_b["mean"] * 100
        lvl = "good" if pct >= -10 else "watch" if pct >= -20 else "flag"
        signals.append({"key": "hrv", "level": lvl,
                        "text": "HRV {:.0f} ms ({:+.0f}% vs avg {:.0f})".format(hrv[d], pct, hrv_b["mean"])})
    if skin is not None and skin_base is not None and skin_sd:
        dev = skin - skin_base
        lvl = "good" if abs(dev) <= 2 * skin_sd else "watch"
        signals.append({"key": "skin_temp", "level": lvl, "text": "Skin temp {:+.1f} °C vs baseline".format(dev)})

    recent = sum(load_by_day.get(x, 0) for x in days_back(day - timedelta(days=1), 3))
    prior = [load_by_day.get(x, 0) for x in days_back(day - timedelta(days=4), BASELINE_DAYS)]
    typical3 = statistics.fmean(prior) * 3 if prior else 0
    if typical3 > 0:
        ratio = recent / typical3
        lvl = "good" if ratio <= 1.5 else "watch"
        signals.append({"key": "load", "level": lvl,
                        "text": "Training last 3 days {} ({:.1f}x your usual)".format(hm(recent), ratio)})

    core = [s["level"] for s in signals if s["key"] in ("sleep", "rhr", "hrv")]
    overall = worst(*core)
    verdict = {"good": "Ready to train", "watch": "Train, but keep it moderate",
               "flag": "Prioritize recovery", "none": "Not enough data yet"}[overall]

    today_workouts = [{
        "type": (w["details"].get("exercise_type") or w["details"].get("display_name") or "Workout"),
        "start": w["start"], "end": w["end"], "minutes": round(w.get("duration") or 0, 1),
        "avg_hr": w.get("average_heart_rate"), "azm": w.get("active_zone_minutes"), "kcal": w.get("calories"),
    } for w in workouts if w["date"] == d]

    week = []
    for x in days_back(day, 7):
        types = [(w["details"].get("exercise_type") or "WORKOUT") for w in workouts if w["date"] == x]
        week.append({"date": x, "minutes": round(load_by_day.get(x, 0)), "types": types})

    is_today = day == datetime.now().astimezone().date()
    return {
        "date": d, "is_today": is_today, "last_sync_at": last_sync(conn),
        "readiness": {"overall": overall, "verdict": verdict, "signals": signals},
        "sleep": None if not main else {
            "start": main["start"], "end": main["end"], "asleep_min": asleep,
            "stages_min": {k: main.get("stage_{}_minutes".format(k)) for k, _, _ in STAGES},
            "naps_min": round(sum((s.get("minutes_asleep") or 0) for s in tonight if s is not main)),
        },
        "vitals": {
            "resting_hr": rhr.get(d), "resting_hr_baseline": rhr_b, "hrv_ms": hrv.get(d), "hrv_baseline": hrv_b,
            "spo2_avg": daily(conn, "spo2_breathing_temperature", "spo2_avg", d, d).get(d),
            "breathing_rate": daily(conn, "spo2_breathing_temperature", "breathing_rate", d, d).get(d),
            "vo2max": daily(conn, "cardio_fitness", "vo2max", d, d).get(d),
            "cardio_fitness_level": daily_text(conn, "cardio_fitness", "cardio_fitness_level", d),
        },
        "activity": {
            "partial_day": is_today, "steps": act("steps"),
            "distance_km": round(act("distance") / 1e6, 2) if act("distance") is not None else None,
            "floors": act("floors"), "calories_out": act("calories_out"), "active_kcal": act("active_energy"),
            "active_min_moderate": act("active_minutes_moderate"), "active_min_vigorous": act("active_minutes_vigorous"),
        },
        "workouts_today": today_workouts,
        "training_week": week,
        "history": {
            "days": days_back(day, history),
            "sleep_min": [asleep_by_night.get(x) for x in days_back(day, history)],
            "steps": [steps.get(x) for x in days_back(day, history)],
            "resting_hr": [rhr.get(x) for x in days_back(day, BASELINE_DAYS)],
            "hrv": [hrv.get(x) for x in days_back(day, BASELINE_DAYS)],
        },
    }


# ---------------------------------------------------------------- rendering

class Ink:
    def __init__(self, theme: str, color: bool):
        self.p, self.color = PALETTES[theme], color

    def __call__(self, text: str, role: str = "ink2", bold: bool = False) -> str:
        if not self.color or not text:
            return text
        hexv = role if role.startswith("#") else self.p[role]
        r, g, b = (int(hexv[i:i + 2], 16) for i in (1, 3, 5))
        return "\033[{}38;2;{};{};{}m{}\033[0m".format("1;" if bold else "", r, g, b, text)


def section(ink: Ink, title: str, note: str = "") -> list[str]:
    head = " " + title.upper() + " "
    tail = (" " + note + " ") if note else ""
    fill = max(2, WIDTH - len(head) - len(tail) - 2)
    return ["", ink("──", "rule") + ink(head, "ink", True) + ink("─" * fill, "rule") + ink(tail, "muted")]


def columns(ink: Ink, days: list[str], values: list, fmt, height: int = 6, goal: float | None = None,
            goal_label: str = "") -> list[str]:
    """Vertical bar chart, 1/8-cell resolution. Missing days show a muted dot, never a zero bar."""
    present = [v for v in values if v is not None]
    top = max(present + ([goal] if goal else []) + [1]) * 1.08
    goal_row = int(goal / top * height) if goal else None
    lines = []
    for row in range(height - 1, -1, -1):
        label = fmt(top) if row == height - 1 else (goal_label if row == goal_row else "")
        cells = []
        for v in values:
            if v is None:
                cells.append(ink("··", "muted") if row == 0 else ("┈┈" if row == goal_row else "  "))
                continue
            level = v / top * height * 8 - row * 8
            if level >= 8:
                cells.append(ink("██", "series"))
            elif level >= 1:
                cells.append(ink(EIGHTHS[int(level)] * 2, "series"))
            elif row == goal_row:
                cells.append(ink("┈┈", "muted"))
            else:
                cells.append("  ")
        gap = ink("┈", "muted") if row == goal_row else " "
        lines.append(ink("{:>7} ".format(label), "muted") + ink("┤" if label else "│", "rule") + " " + gap.join(cells))
    lines.append(" " * 8 + ink("└" + "─" * (len(values) * 3), "rule"))
    letters = []
    for i, x in enumerate(days):
        dl = date.fromisoformat(x).strftime("%a")[:2]
        letters.append(ink(dl, "ink", True) if i == len(days) - 1 else ink(dl, "muted"))
    lines.append(" " * 10 + " ".join(letters))
    return lines


def spark(ink: Ink, values: list, lower_is_better: bool = False) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return ink("no data", "muted")
    lo, hi = min(present), max(present)
    out = []
    for i, v in enumerate(values):
        if v is None:
            out.append(ink("·", "muted"))
            continue
        idx = 1 + int((v - lo) / (hi - lo) * 7) if hi > lo else 4
        out.append(ink(EIGHTHS[idx], "ink" if i == len(values) - 1 else "series", i == len(values) - 1))
    return "".join(out)


def chip(ink: Ink, level: str, text: str) -> str:
    color, icon = STATUS[level]
    return ink(icon, color, True) + " " + ink(text, "ink2")


def render(dg: dict, ink: Ink, history: int) -> str:
    day = date.fromisoformat(dg["date"])
    L: list[str] = []
    synced = clock(dg["last_sync_at"]) if dg["last_sync_at"] else "never"
    title = " DAILY HEALTH BRIEF · {} ".format(day.strftime("%a %b %-d, %Y"))
    note = " synced {} ".format(synced)
    L.append(ink("╭─", "rule") + ink(title, "ink", True) + ink("─" * (WIDTH - len(title) - len(note) - 4), "rule")
             + ink(note, "muted") + ink("─╮", "rule"))

    # readiness
    r = dg["readiness"]
    color, icon = STATUS[r["overall"]]
    L.append("")
    L.append("  " + ink(" {} {} ".format(icon, r["verdict"].upper()), color, True))
    for s in r["signals"]:
        L.append("    " + chip(ink, s["level"], s["text"]))

    # sleep
    sl = dg["sleep"]
    if sl:
        L += section(ink, "Sleep · last night", "{} → {}".format(clock(sl["start"]), clock(sl["end"])))
        L.append("  " + ink(hm(sl["asleep_min"]), "ink", True) + ink(" asleep", "muted")
                 + (ink("  + {} nap".format(hm(sl["naps_min"])), "muted") if sl["naps_min"] else ""))
        stages = [(k, name, g, sl["stages_min"].get(k) or 0) for k, name, g in STAGES]
        total = sum(m for *_, m in stages) or 1
        width = WIDTH - 4
        raw = [m / total * width for *_, m in stages]
        cells = [int(x) for x in raw]
        for i in sorted(range(len(raw)), key=lambda i: raw[i] - cells[i], reverse=True)[:width - sum(cells)]:
            cells[i] += 1
        L.append("  " + "".join(ink(g * n, k) for (k, _, g, _), n in zip(stages, cells)))
        legend = []
        for k, name, g, m in stages:
            legend.append(ink(g, k) + " " + ink(name, "ink2") + " " + ink("{} {:.0f}%".format(hm(m), m / total * 100), "muted"))
        L.append("  " + "   ".join(legend))
        h = dg["history"]
        L.append("")
        L.append(ink("  hours asleep · last {} nights".format(history), "muted"))
        L += columns(ink, h["days"], [None if v is None else v / 60 for v in h["sleep_min"]],
                     lambda v: "{:.0f}h".format(v), goal=7, goal_label="7h")

    # heart
    v, h = dg["vitals"], dg["history"]
    L += section(ink, "Heart & recovery", "last {} days".format(BASELINE_DAYS))
    rb, hb = v["resting_hr_baseline"], v["hrv_baseline"]
    rhr_vals = [x for x in h["resting_hr"] if x is not None]
    hrv_vals = [x for x in h["hrv"] if x is not None]
    if rhr_vals:
        L.append("  " + ink("{:<12}".format("Resting HR"), "ink2") + spark(ink, h["resting_hr"]) + "  "
                 + ink("{:.0f} bpm".format(v["resting_hr"]) if v["resting_hr"] else "—", "ink", True)
                 + ink("  range {:.0f}–{:.0f}{}".format(min(rhr_vals), max(rhr_vals),
                                                      ", avg {}".format(rb["mean"]) if rb else ""), "muted"))
    if hrv_vals:
        L.append("  " + ink("{:<12}".format("HRV"), "ink2") + spark(ink, h["hrv"]) + "  "
                 + ink("{:.0f} ms".format(v["hrv_ms"]) if v["hrv_ms"] else "—", "ink", True)
                 + ink("  range {:.0f}–{:.0f}{}".format(min(hrv_vals), max(hrv_vals),
                                                      ", avg {:.0f}".format(hb["mean"]) if hb else ""), "muted"))
    extras = []
    if v["spo2_avg"]:
        extras.append("SpO₂ {:.1f}%".format(v["spo2_avg"]))
    if v["breathing_rate"]:
        extras.append("breathing {:.1f}/min".format(v["breathing_rate"]))
    if v["vo2max"]:
        lvl = (v["cardio_fitness_level"] or "").replace("_", " ").lower()
        extras.append("VO₂ max {:.1f}{}".format(v["vo2max"], " ({})".format(lvl) if lvl else ""))
    if extras:
        L.append("  " + ink(" · ".join(extras), "muted"))
    L.append(ink("  ▁ low … █ high within the window · ends today · '·' = no reading", "muted"))

    # activity
    a = dg["activity"]
    L += section(ink, "Activity", "so far today" if a["partial_day"] else "")
    tiles = []
    if a["steps"] is not None:
        tiles.append(("{:,.0f}".format(a["steps"]), "steps"))
    if a["distance_km"] is not None:
        tiles.append(("{:.1f} km".format(a["distance_km"]), "distance"))
    if a["active_kcal"] is not None:
        tiles.append(("{:,.0f}".format(a["active_kcal"]), "active kcal"))
    if a["floors"] is not None:
        tiles.append(("{:.0f}".format(a["floors"]), "floors"))
    act_min = (a["active_min_moderate"] or 0) + (a["active_min_vigorous"] or 0)
    tiles.append(("{:.0f}".format(act_min), "mod+vig min"))
    L.append("  " + "".join(ink("{:<14}".format(val), "ink", True) for val, _ in tiles))
    L.append("  " + "".join(ink("{:<14}".format(lab), "muted") for _, lab in tiles))
    L.append("")
    L.append(ink("  steps · last {} days".format(history), "muted"))
    L += columns(ink, h["days"], h["steps"], lambda v: "{:.0f}k".format(v / 1000), height=5)

    # training
    L += section(ink, "Training · last 7 days")
    wk = dg["training_week"]
    peak = max([w["minutes"] for w in wk] + [60])
    for w in wk:
        dd = date.fromisoformat(w["date"])
        bar_len = w["minutes"] / peak * 30
        bar = "█" * int(bar_len) + (EIGHTHS[int((bar_len % 1) * 8)] if bar_len % 1 >= 0.125 else "")
        kinds = sorted(set(SHORT.get(t, t.replace("_", " ").title()) for t in w["types"]))
        is_day = w["date"] == dg["date"]
        idle = "none yet" if is_day and dg["is_today"] else "rest"
        L.append("  " + ink(dd.strftime("%a %m/%d"), "ink" if is_day else "muted", is_day) + "  "
                 + ink("{:<31}".format(bar) if w["minutes"] else "{:<31}".format("·"), "series" if w["minutes"] else "muted")
                 + ink("{:>7}".format(hm(w["minutes"]) if w["minutes"] else idle), "ink2")
                 + "  " + ink(", ".join(kinds)[:24], "muted"))
    if dg["workouts_today"]:
        L.append("")
        L.append(ink("  today", "muted"))
        for w in dg["workouts_today"]:
            L.append("    " + ink("{:<17}".format(clock(w["start"]) + "–" + clock(w["end"]).replace(" ", "")), "ink2")
                     + ink("{:<18}".format(w["type"].replace("_", " ").title()), "ink", True)
                     + ink("{:>7}".format(hm(w["minutes"])), "ink2")
                     + ink("{:>9}".format("{:.0f} bpm".format(w["avg_hr"]) if w["avg_hr"] else ""), "muted")
                     + ink("{:>8}".format("{:.0f} AZM".format(w["azm"]) if w["azm"] is not None else ""), "muted")
                     + ink("{:>9}".format("{:.0f} kcal".format(w["kcal"]) if w["kcal"] else ""), "muted"))

    L.append("")
    L.append(ink("╰" + "─" * (WIDTH - 2) + "╯", "rule"))
    L.append(ink("  Fitbit / Google Health estimates · missing days are gaps, not zeros · not medical advice", "muted"))
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, local time)")
    ap.add_argument("--days", type=int, default=14, help="history window for the column charts (7-21)")
    ap.add_argument("--theme", choices=("dark", "light"), default=os.environ.get("HEALTH_BRIEF_THEME", "dark"))
    ap.add_argument("--plain", action="store_true", help="no ANSI color")
    ap.add_argument("--json", action="store_true", help="print the digest as JSON instead of charts")
    args = ap.parse_args()

    day = date.fromisoformat(args.date) if args.date else datetime.now().astimezone().date()
    history = min(max(args.days, 7), 21)
    with connect() as conn:
        dg = build_digest(conn, day, history)
    if args.json:
        print(json.dumps(dg, indent=1, default=str))
        return
    color = not args.plain and "NO_COLOR" not in os.environ
    print(render(dg, Ink(args.theme, color), history))


if __name__ == "__main__":
    main()
