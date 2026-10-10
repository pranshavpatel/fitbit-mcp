"""Coach notes: a short, WHOOP-coach-style message at the top of the Today box.

    fitdash coach run                 write a note if one is due (morning / after a workout / evening)
    fitdash coach run --kind morning --force      write one now regardless
    fitdash coach run --dry-run       show what would be sent and written, change nothing
    fitdash coach set <kind> "text"   save a note you (or Claude in a chat) wrote
    fitdash coach show                today's notes
    fitdash coach --install | --uninstall         check every 30 min (launchd)

When a note is due:
  morning   05:00-12:00, once last night's sleep is in (or from 10:30 regardless), once a day
  activity  a workout that ended in the last 4 h and hasn't had a note yet
  evening   from 21:00 (or 90 min before tonight's asleep-by time, not before 20:00), once a day

Notes are written by Claude through the `claude` CLI in print mode with no tools and no MCP
servers: it receives a detailed summary (today, the last 7 days, training, body, nutrition, journal,
earlier notes and your coaching profile) and returns text, nothing else. The user has said they're
happy to share their health data, so the summary is as complete as is useful. If the
CLI isn't available or fails, the Today box shows a rule-based note instead (marked "auto").
Notes are stored in ~/.fitbit-mcp/coaching/notes.json.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tz import local_zone

NY = local_zone()      # the display timezone (tz.py); named NY for history
KINDS = ("morning", "midday", "activity", "evening")
KIND_TITLE = {"morning": "Morning", "midday": "Midday check-in", "activity": "After your workout",
              "evening": "Closing the day", "note": "Note"}
MIDDAY = (13 * 60, 16 * 60)       # the priorities check-in, once, if today's priorities are still open
LABEL = "com.fitdash.coach"
MAX_WORDS = 75
KEEP = 90                         # notes kept in the file

SYSTEM = """You are the user's personal health and training coach, in the style of the WHOOP coach.
You write one short note that appears at the top of their terminal health dashboard.

Rules:
- At most {max_words} words, plain text, 2-4 sentences. No greeting, no sign-off, no markdown,
  no emoji, no bullet points, no filler. Direct and specific, like a good coach texting.
- Lead with the one thing that matters most right now, citing 1-2 of the actual numbers.
- Then give 1-2 concrete actions for the rest of today or tonight (what, how much, when).
- Use the user's profile: their goals, split, bedtime goal, knee note. If an impact spike or knee
  pain is flagged, never suggest running or jumping.
- Only state what the data supports. Missing data is missing, not zero. Recovery, Strain and
  Sleep scores are estimates on WHOOP-like scales, not WHOOP's own.
- Not medical advice: for anything worrying or persistent, suggest a clinician in a few words.
- Use the 7-day trends and earlier notes: point out a pattern when it matters, and don't repeat
  what an earlier note today already said.
- If `sleep_sync_note` is present, last night may not have finished uploading: don't judge the
  night or the recovery score from it; say it may still be syncing and ask them to open the Fitbit
  app, then check again.
- The user's top 3 priorities for the day (`priorities`) are the point of the day; health serves
  them. Help them be intentional: name a priority by its words, link it to their energy, and be
  encouraging but honest. Never guilt-trip. If none are set, ask them to pick 3
  (`fitdash priorities`, or the phone app's Log tab).

Note types:
- morning: how they recovered and slept, and what today should look like (training intensity
  vs recovery, the split day, steps). If priorities are set, say when to tackle the hardest one
  given their recovery (e.g. green: do it first; red: protect a focused block, keep training light).
- midday: a short check-in on the priorities still open: which one to move forward next, with a
  concrete next step and a time block, plus any health nudge that helps (food, a walk, caffeine
  cut-off). If one is already done, acknowledge it.
- activity: react to the workout that just ended (load, zones, how it fits today's target and the
  week, recovery/fueling/sleep implications). Name the workout.
- evening: close the day. Ask how the priorities went (they can mark them with `fitdash priorities`
  or in the app), credit what got done, and turn anything missed into one small, specific step for
  tomorrow. Then health: strain vs target and tonight's asleep-by time.
"""


# ---------------------------------------------------------------- storage

def notes_path(home: Path) -> Path:
    return home / "coaching" / "notes.json"


def load(home: Path) -> list[dict]:
    try:
        data = json.loads(notes_path(home).read_text())
    except (OSError, ValueError):
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    return [n for n in (notes or []) if isinstance(n, dict) and isinstance(n.get("text"), str)
            and isinstance(n.get("date"), str) and isinstance(n.get("at"), str)]


def save(home: Path, notes: list[dict]) -> None:
    p = notes_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "notes": notes[-KEEP:]}, indent=1) + "\n")
    tmp.replace(p)


def add(home: Path, kind: str, text: str, now: datetime, source: str, trigger: str | None = None) -> dict:
    note = {"date": coach_day(now)[0], "at": now.strftime("%Y-%m-%dT%H:%M"), "kind": kind,
            "text": clean(text), "source": source}
    if trigger:
        note["trigger"] = trigger
    notes = load(home)
    notes.append(note)
    save(home, notes)
    return note


def clean(text: str) -> str:
    """One paragraph of plain text: strip markdown, quotes and stray whitespace; cap the length."""
    t = re.sub(r"[*_`#>]+", "", text or "").strip().strip('"').strip()
    t = " ".join(t.split())
    words = t.split(" ")
    if len(words) > MAX_WORDS + 25:
        t = " ".join(words[:MAX_WORDS + 25]).rstrip(",;:") + "…"
    return t


# ---------------------------------------------------------------- when is a note due

DAY_ROLLOVER_H = 4                # until 04:00 it's still "last night": the evening belongs to the day before


def coach_day(now: datetime) -> tuple[str, int]:
    """(the day a note is about, minutes since that day's midnight). 00:30 → yesterday, 1470."""
    mins = now.hour * 60 + now.minute
    if now.hour < DAY_ROLLOVER_H:
        return (now.date() - timedelta(days=1)).isoformat(), mins + 1440
    return now.date().isoformat(), mins

def _local(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts[:19]).replace(tzinfo=NY)
    except ValueError:
        return None


def _workouts_today(m: dict) -> list[dict]:
    return [w for w in (m.get("workouts") or {}).get("week", []) if w.get("date") == m.get("date")]


def _evening_start(m: dict) -> float:
    tn = (m.get("sleep") or {}).get("tonight") or {}
    start = 21 * 60
    if tn.get("asleep_by") is not None:
        ab = tn["asleep_by"] % 1440
        ab = ab + 1440 if ab < 12 * 60 else ab        # a past-midnight asleep-by belongs to tonight
        start = min(start, ab - 90)
    return max(20 * 60, start)


def due(m: dict, notes: list[dict], now: datetime) -> tuple[str, dict | None] | None:
    """(kind, workout or None) for the note that's due now, or None. Activity first, then evening,
    then morning. A day's morning and evening notes are written once; each workout once."""
    if not m.get("has_data"):
        return None
    today, mins = coach_day(now)
    todays = [n for n in notes if n.get("date") == today]
    kinds_done = {n.get("kind") for n in todays}
    triggers = {n.get("trigger") for n in notes if n.get("trigger")}
    last_at = max((_local(n["at"]) for n in notes if _local(n.get("at"))), default=None)
    for w in sorted(_workouts_today(m), key=lambda w: w["end"], reverse=True):
        end = _local(w.get("end"))
        if not end or (w.get("minutes") or 0) < 10 or "workout:" + w["start"] in triggers:
            continue
        if last_at and last_at >= end:             # a later note (e.g. the evening one) already covered it
            continue
        if timedelta(0) <= now - end <= timedelta(hours=4):
            return "activity", w
    if mins >= _evening_start(m) and "evening" not in kinds_done:
        return "evening", None
    items = (((m.get("priorities") or {}).get("today")) or {}).get("items") or []
    if MIDDAY[0] <= mins < MIDDAY[1] and "midday" not in kinds_done and any(i.get("status") is None for i in items):
        return "midday", None
    if 5 * 60 <= mins < 12 * 60 and "morning" not in kinds_done:
        if (m.get("sleep") or {}).get("has_night") or mins >= 10 * 60 + 30:
            return "morning", None
    return None


# ---------------------------------------------------------------- what the coach sees

def _hm(minutes) -> str | None:
    if minutes is None:
        return None
    return "{}h{:02d}".format(int(minutes // 60), int(round(minutes % 60)))


def _clock(minutes) -> str | None:
    if minutes is None:
        return None
    m_ = int(round(minutes)) % 1440
    return "{}:{:02d}".format(m_ // 60, m_ % 60)


def context(m: dict, kind: str, workout: dict | None, now: datetime, profile: str = "",
            earlier: list[dict] | None = None) -> dict:
    """Everything a coach would want: today in detail, the last 7 days, training, body and
    nutrition, journal and earlier notes (so it doesn't repeat itself). Summaries, not raw samples."""
    sl, rec, st, tr = m.get("sleep") or {}, m.get("recovery") or {}, m.get("strain") or {}, m.get("training") or {}
    sc = sl.get("score") or {}
    ctx = {
        "note_type": kind, "now": now.strftime("%A %Y-%m-%d %H:%M"),
        "verdict": (m.get("verdict") or {}).get("headline"),
        "recovery": {"score": rec.get("score"), "band": rec.get("band"),
                     "drivers": [{"what": c["label"], "points": round(c["points"])} for c in rec.get("contributions", [])
                                 if abs(c.get("points") or 0) >= 1]},
        "baseline_28d": {k: (m.get("baselines", {}).get(k) or {}).get("mean")
                         for k in ("recovery", "hrv", "rhr", "sleep_score", "strain", "steps")},
        "hrv_ms": (m.get("series") or {}).get("hrv", [None])[-1], "resting_hr": (m.get("series") or {}).get("rhr", [None])[-1],
        "last_night": None if not sl.get("has_night") else {
            "sleep_score": sc.get("score"), "band": sc.get("band"), "asleep": _hm(sl.get("asleep")),
            "need": _hm((sl.get("need") or {}).get("total")), "efficiency_pct": sl.get("efficiency"),
            "consistency": sl.get("consistency"), "bed": (sl.get("start") or "")[11:16], "wake": (sl.get("end") or "")[11:16],
            "deep": _hm((sl.get("stages") or {}).get("deep")), "rem": _hm((sl.get("stages") or {}).get("rem"))},
        "strain_today": {"so_far": st.get("day"), "target": st.get("target")},
        "steps_today": (m.get("activity") or {}).get("steps"), "steps_goal": (m.get("config") or {}).get("steps_goal"),
        "workouts_today": [{"what": w["label"], "start": w["start"][11:16], "minutes": round(w["minutes"]),
                            "strain": w.get("strain"), "avg_hr": w.get("avg_hr"), "zone": w.get("zone"),
                            "km": w.get("distance_km")} for w in _workouts_today(m)],
        "training": {"gym_this_week": tr.get("gym_week"), "gym_goal": tr.get("gym_goal"),
                     "next_split": (tr.get("split") or {}).get("next"),
                     "next_split_fresh_pct": ((tr.get("split") or {}).get("fresh") or {}).get((tr.get("split") or {}).get("next")),
                     "load_ratio": (tr.get("acwr") or {}).get("ratio"), "load_zone": (tr.get("acwr") or {}).get("zone"),
                     "impact_spike_knee": tr.get("impact_spike_recent"), "impact_limit_min_week": tr.get("impact_limit")},
        "tonight": {"asleep_by": _clock(((sl.get("tonight") or {}).get("asleep_by"))),
                    "need": _hm((((sl.get("tonight") or {}).get("need")) or {}).get("total")),
                    "sleep_debt": _hm((sl.get("tonight") or {}).get("debt")),
                    "wake": _clock((sl.get("tonight") or {}).get("wake"))},
        "needs_attention": [t for lvl, t in (m.get("verdict") or {}).get("reasons", []) if lvl in ("watch", "flag")],
    }
    exp = m.get("experiment")
    if exp:
        ctx["sleep_experiment"] = {"name": exp.get("name"), "lights_out": exp.get("lights_out"),
                                   "nights": "{}/{} on target".format(sum(1 for r in exp.get("nights", []) if r.get("state") == "on target"),
                                                                     sum(1 for r in exp.get("nights", []) if r.get("state") != "upcoming"))}
    lf = m.get("lifting") or {}
    if lf.get("has_log"):
        ctx["lifting_7d"] = {"sets_per_muscle": {k: v for k, v in lf["sets"].items() if v},
                             "under_10_sets": lf.get("under"), "target": lf.get("target")}
    j = m.get("journal") or {}
    if j.get("has_log"):
        ctx["journal_7d"] = {r["label"]: "{}/{} days".format(r["yes7"], r["logged7"]) for r in j.get("rows", []) if r["logged7"]}
        if j.get("last_note"):
            ctx["journal_last_note"] = j["last_note"]
    effects = [e for e in (m.get("insights") or {}).get("effects", []) if e["strength"] != "unclear"][:3]
    if effects:
        ctx["personal_patterns"] = ["{}: next-morning recovery {:+.0f} ({} vs {}, {})".format(
            e["label"], e["diff"], e["mean_yes"], e["mean_no"], e["strength"]) for e in effects]
    if workout:
        ctx["this_workout"] = {"what": workout["label"], "start": workout["start"][11:16], "end": workout["end"][11:16],
                               "minutes": round(workout["minutes"]), "strain": workout.get("strain"),
                               "avg_hr": workout.get("avg_hr"), "max_hr": workout.get("max_hr"),
                               "zone_minutes": workout.get("zone_minutes"), "km": workout.get("distance_km"),
                               "pace_s_per_km": workout.get("pace_s_per_km"), "kcal": workout.get("kcal"),
                               "split_day": workout.get("split")}
    # ---- fuller picture (the user is happy to share their data; more context → better notes)
    ser, days = m.get("series") or {}, m.get("days") or []
    def last(key, n=7):
        return (ser.get(key) or [])[-n:]
    ctx["last_7_days"] = [{"date": d_, "recovery": r, "sleep_score": ss, "asleep": _hm(a), "hrv": h, "rhr": rr,
                           "strain": None if s_ is None else round(s_, 1), "steps": st_}
                          for d_, r, ss, a, h, rr, s_, st_ in zip(days[-7:], last("recovery"), last("sleep_score"),
                                                                  last("asleep"), last("hrv"), last("rhr"),
                                                                  last("strain"), last("steps"))]
    ctx["sleep_timing_7"] = [{"night": t["date"], "bed": (t.get("start") or "")[11:16] or None,
                              "wake": (t.get("end") or "")[11:16] or None, "asleep": _hm(t.get("asleep"))}
                             for t in (sl.get("timing") or [])[-7:]]
    if sl.get("has_night"):
        ctx["last_night"].update({"light": _hm((sl.get("stages") or {}).get("light")),
                                  "awake": _hm((sl.get("stages") or {}).get("awake")),
                                  "awakenings": sl.get("awake_count"), "sleep_stress_pct": sl.get("stress_pct"),
                                  "score_parts": {k: v.get("score") for k, v in ((sc.get("components") or {}).items())}})
    ctx["workouts_7d"] = [{"date": w["date"], "what": w["label"], "split": w.get("split"), "start": w["start"][11:16],
                           "minutes": round(w["minutes"]), "strain": w.get("strain"), "avg_hr": w.get("avg_hr"),
                           "zone": w.get("zone"), "km": w.get("distance_km")}
                          for w in (m.get("workouts") or {}).get("week", [])]
    a = m.get("activity") or {}
    ctx["activity_today"] = {k: a.get(k) for k in ("steps", "steps_avg", "active_min", "azm", "active_kcal",
                                                   "cal_out", "sedentary_min", "floors")}
    h = m.get("heart") or {}
    ctx["heart_today"] = {"zones_min": h.get("zones_today"), "resp_rate": h.get("resp"), "spo2": h.get("spo2"),
                          "skin_temp_dev_c": h.get("skin_dev"), "vo2max_level": h.get("vo2_level")}
    b = m.get("body") or {}
    ctx["body"] = {"latest_kg": b.get("latest_kg"), "days_since_weigh_in": b.get("days_since_weigh_in"),
                   "trend_kg_per_week": (b.get("trend") or {}).get("kg_per_week"),
                   "lean_bulk_pace": (b.get("trend") or {}).get("status"), "pace_target_kg_wk": b.get("pace_target_kg"),
                   "calories_in_today": b.get("cal_in_today"), "macros_today_g": b.get("macros"),
                   "last_food_log": b.get("last_food_log")}
    lf = m.get("lifting") or {}
    if lf.get("lifts"):
        ctx["lift_progress"] = [{"exercise": r["exercise"], "last": r["last"]["top"], "date": r["last"]["date"],
                                 "e1rm_kg": r["last"]["e1rm"], "change_pct_8w": r["change_pct"], "pr": r["pr"]}
                                for r in lf["lifts"][:8]]
    j = m.get("journal") or {}
    if j.get("has_log"):
        ctx["journal_last_3_days"] = {d_: {r["label"]: r["cells"][-3:][i] for r in j.get("rows", [])
                                          if r["cells"][-3:][i] is not None}
                                      for i, d_ in enumerate((j.get("days") or [])[-3:])}
    wk = m.get("week_review") or {}
    if wk.get("rows"):
        ctx["this_week_vs_last"] = {r["metric"]: {"now": r["avg"], "prev": r["prev"]} for r in wk["rows"] if r["avg"] is not None}
    if sl.get("sync_note"):
        ctx["sleep_sync_note"] = sl["sync_note"]["text"]
    pr = m.get("priorities") or {}
    today_pr = pr.get("today") or {}
    ctx["priorities"] = {
        "today": [{"text": i["text"], "status": i.get("status") or "open"} for i in today_pr.get("items", [])] or None,
        "reflection": today_pr.get("reflection") or None,
        "last_7_days": {k: (pr.get("week") or {}).get(k) for k in ("set_days", "items", "done", "partial", "missed", "streak")},
        "recent": [{"date": d_["date"], "items": [{"text": i["text"], "status": i.get("status") or "open"} for i in d_["items"]]}
                   for d_ in (pr.get("week") or {}).get("by_day", [])[-4:-1] if d_["items"]],
    }
    if earlier:
        ctx["earlier_notes_today"] = [{"kind": n["kind"], "at": n["at"][11:], "text": n["text"]} for n in earlier]
    if profile.strip():
        ctx["profile"] = profile.strip()[:4000]
    return ctx


def prompt(ctx: dict) -> str:
    return ("Write the {} note now. Today's data (JSON):\n{}".format(ctx["note_type"], json.dumps(ctx, default=str)))


def claude_bin() -> str | None:
    for cand in (shutil.which("claude"), str(Path.home() / ".local" / "bin" / "claude")):
        if cand and Path(cand).exists():
            return cand
    return None


def generate(ctx: dict, timeout: float = 150) -> str | None:
    """Ask Claude for the note: print mode, no tools, no MCP servers, no session saved. Returns the
    text, or None if the CLI isn't there or fails."""
    exe = claude_bin()
    if not exe:
        return None
    cmd = [exe, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "user",
           "--output-format", "text", "--system-prompt", SYSTEM.format(max_words=MAX_WORDS)]
    try:
        r = subprocess.run(cmd, input=prompt(ctx), capture_output=True, text=True, timeout=timeout,
                           cwd=str(Path.home()))
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = clean(r.stdout)
    return text if r.returncode == 0 and len(text.split()) >= 5 else None


# ---------------------------------------------------------------- what the Today box shows

def kind_for(now: datetime) -> str:
    mins = now.hour * 60 + now.minute
    return "morning" if 4 * 60 <= mins < 12 * 60 else "evening" if mins >= 20 * 60 or mins < 4 * 60 else "day"


def pick(notes: list[dict], day: str, now: datetime) -> dict | None:
    """The newest note for `day`; before 05:00 last night's evening note still counts."""
    cands = [n for n in notes if n.get("date") == day]
    if not cands and now.hour < DAY_ROLLOVER_H + 1:
        prev = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
        cands = [n for n in notes if n.get("date") == prev and n.get("kind") == "evening"]
    return max(cands, key=lambda n: n["at"]) if cands else None


def fallback(m: dict, now: datetime) -> str:
    """A rule-based note from the same numbers, for when no written note exists yet."""
    rec, sl, st = m.get("recovery") or {}, m.get("sleep") or {}, m.get("strain") or {}
    tn = sl.get("tonight") or {}
    parts = []
    k = kind_for(now)
    sc = (sl.get("score") or {}).get("score")
    items = ((m.get("priorities") or {}).get("today") or {}).get("items") or []
    if k == "evening" and items and any(not i.get("status") for i in items):
        parts.append("How did your priorities go? Mark them with fitdash priorities.")
    if k == "morning" and not items:
        parts.append("Set today's top 3 priorities: fitdash priorities.")
    if k == "evening":
        if st.get("day") is not None and st.get("target"):
            lo, hi = st["target"]
            where = "inside" if lo <= st["day"] <= hi else "under" if st["day"] < lo else "over"
            parts.append("Strain {:.1f}, {} today's {:.0f}–{:.0f} target.".format(st["day"], where, lo, hi))
        if tn.get("asleep_by") is not None:
            ab = tn["asleep_by"] % 1440
            ab = ab + 1440 if ab < 12 * 60 else ab
            mins = now.hour * 60 + now.minute + (1440 if now.hour < 12 else 0)
            need = _hm((tn.get("need") or {}).get("total"))
            if mins > ab:
                parts.append("Past your {} asleep-by time: lights out now, every hour counts toward the {} need."
                             .format(_clock(ab), need))
            else:
                parts.append("Aim to be asleep by {} for your {} need; screens off 30 min before.".format(_clock(ab), need))
    elif k == "day" and ((m.get("priorities") or {}).get("today") or {}).get("items"):
        open_ = [i["text"] for i in m["priorities"]["today"]["items"] if not i.get("status")]
        if open_:
            parts.append("Still open: {}. Pick one and give it a focused block now.".format("; ".join(open_)))
    else:
        if rec.get("score") is not None:
            parts.append("Recovery {}%{}.".format(rec["score"], "" if sc is None else ", sleep score {}".format(sc)))
        v = (m.get("verdict") or {}).get("headline")
        if v:
            parts.append(v.rstrip(".") + ".")
        nxt = ((m.get("training") or {}).get("split") or {}).get("next")
        if nxt:
            parts.append("Next up: {}.".format(nxt))
    return " ".join(parts) or "Not enough data for a note yet."


# ---------------------------------------------------------------- launchd

def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def install(home: Path, fitdash: list[str], path_env: str, every_min: int = 30) -> int:
    log = str(home / "coach.log")
    plist = {"Label": LABEL, "ProgramArguments": fitdash + ["coach", "run"], "StartInterval": every_min * 60,
             "RunAtLoad": True, "EnvironmentVariables": {"PATH": path_env},
             "StandardOutPath": log, "StandardErrorPath": log}
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    domain = "gui/{}".format(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    with open(p, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        print("launchctl bootstrap failed: {}".format(r.stderr.strip()), file=sys.stderr)
        return 1
    print("coach checks every {} min for a due note (morning / after workouts / evening); log {}".format(every_min, log))
    if not claude_bin():
        print("  note: the `claude` CLI wasn't found, so notes will be rule-based until it is", file=sys.stderr)
    print("  remove with: fitdash coach --uninstall")
    return 0


def uninstall() -> int:
    subprocess.run(["launchctl", "bootout", "gui/{}/{}".format(os.getuid(), LABEL)], capture_output=True)
    if plist_path().exists():
        plist_path().unlink()
    print("coach schedule removed")
    return 0
