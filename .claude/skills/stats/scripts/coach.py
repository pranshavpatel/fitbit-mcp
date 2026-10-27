"""Coach notes: a short, WHOOP-coach-style message at the top of the Today box.

    fitdash coach run                 write a note if one is due (morning / after a workout / evening)
    fitdash coach run --kind morning --force      write one now regardless
    fitdash coach run --dry-run       show what would be sent and written, change nothing
    fitdash coach set <kind> "text"   save a note you (or Claude in a chat) wrote
    fitdash coach show                today's notes
    fitdash coach --install | --uninstall         check every 30 min (launchd)
    fitdash insights [--no-sync] [--dry-run] [--last] [--json]
                                      on demand: actionable insights on today and on the past week

`fitdash insights` (also `fitdash coach insights`) reads every stat fitdash has and returns two
sets of insights, each a finding with the numbers behind it plus one concrete action: TODAY (this
day's recovery, sleep, strain, steps, food and priorities) and PAST 7 DAYS (trends, patterns and
what to change next week). The last Claude-written result is kept in
~/.fitbit-mcp/coaching/insights.json (`--last` shows it); rule-based fallbacks are not saved.

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


def ask(system: str, user: str, timeout: float = 150) -> str | None:
    """One Claude call: print mode, no tools, no MCP servers, no session saved. Returns stdout, or
    None if the CLI isn't there or fails."""
    exe = claude_bin()
    if not exe:
        return None
    cmd = [exe, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "user",
           "--output-format", "text", "--system-prompt", system]
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout, cwd=str(Path.home()))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def generate(ctx: dict, timeout: float = 150) -> str | None:
    """Ask Claude for the note. Returns the text, or None if the CLI isn't there or fails."""
    text = clean(ask(SYSTEM.format(max_words=MAX_WORDS), prompt(ctx), timeout) or "")
    return text if len(text.split()) >= 5 else None


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


# ---------------------------------------------------------------- on-demand insights

INSIGHTS_SYSTEM = """You are the user's personal health and training coach. They asked for insights on
their day and their week. You get every stat their dashboard has (JSON). Reply with JSON only, no
prose around it, in exactly this shape:

{"today": {"headline": "...", "insights": [{"level": "good|watch|flag", "finding": "...", "action": "..."}]},
 "week":  {"headline": "...", "insights": [{"level": "good|watch|flag", "finding": "...", "action": "..."}]}}

- today: about the current day only (`now`, `last_night`, `recovery`, `strain_today`, `activity_today`,
  `workouts_today`, `tonight`, `body` food today, `priorities.today`). 3-4 insights.
- week: the past 7 days (`last_7_days`, `this_week`, `workouts_7d`, `sleep_timing_7`, `lifting_7d`,
  `food_7d`, `weights_recent`, `journal_7d`, `priorities.last_7_days`, `personal_patterns`). 3-5
  insights: trends vs the week before, what drove the good and bad days, and what to change next week.
- headline: one sentence, at most 16 words, the single most important takeaway.
- finding: what the data shows, citing the actual numbers (dates as weekdays). At most 30 words.
  Explain why it matters when that isn't obvious (e.g. a pattern from `personal_patterns`).
- action: one concrete thing to do, with an amount and a time (e.g. "In bed by 22:30 tonight: you
  need 10h00"). At most 25 words. Prefer the few changes that would move the most.
- level: good = going well, keep it; watch = drifting; flag = needs action now. Order by importance.
- Use the profile (goals, split, bedtime goal, knee note). If an impact spike or knee pain is
  flagged, never suggest running or jumping.
- Only state what the data supports. Missing data is missing, not zero; if a day looks partially
  synced (e.g. very few steps, `sleep_sync_note`), say so instead of judging it. Today is still in
  progress: judge strain and steps so far against the time of day. Recovery, Strain and Sleep scores
  are estimates on WHOOP-like scales, not WHOOP's own.
- Don't repeat the same point in both sections. Plain text in the strings, no markdown, no emoji.
- Not medical advice: for anything worrying or persistent, suggest a clinician in a few words.
"""

INSIGHT_LEVELS = ("good", "watch", "flag")


def insights_context(m: dict, now: datetime, profile: str = "", earlier: list[dict] | None = None,
                     food_days: dict | None = None) -> dict:
    """The note context plus the whole week: the week-vs-last table, attention items, muscle
    freshness, recent weigh-ins and the food log."""
    ctx = context(m, "insights", None, now, profile, earlier)
    ctx.pop("note_type", None)
    wk = m.get("week_review") or {}
    if wk.get("rows"):
        ctx.pop("this_week_vs_last", None)
        ctx["this_week"] = {"days": wk.get("days"), "today_excluded_from_avg": wk.get("excluded_today"), "metrics": [
            {"metric": r["metric"], "avg": None if r["avg"] is None else round(r["avg"], 1),
             "prev_week_avg": None if r["prev"] is None else round(r["prev"], 1),
             "daily": [None if v is None else round(v, 1) for v in r.get("series") or []],
             "best": r.get("best"), "worst": r.get("worst")} for r in wk["rows"]]}
    ctx["needs_attention"] = [t for lvl, t in (m.get("attention") or []) if lvl in ("watch", "flag")] or ctx["needs_attention"]
    mus = (m.get("muscles") or {}).get("rows") or []
    if any(r.get("last") for r in mus):
        ctx["muscle_freshness_pct"] = {r["muscle"]: r["fresh"] for r in mus}
    b = m.get("body") or {}
    ctx["weights_recent"] = [w for w in (b.get("weights") or [])[-6:]]
    ctx["body"]["protein_target_g"] = b.get("protein_target_g")
    if food_days:
        days = set((wk.get("days") or [])) or {(now.date() - timedelta(days=i)).isoformat() for i in range(7)}
        import food as F
        ctx["food_7d"] = {d_: F.totals(e) for d_, e in sorted(food_days.items()) if d_ in days} or None
    tr = m.get("training") or {}
    ctx["training"]["split_queue"] = (tr.get("split") or {}).get("queue")
    ctx["training"]["last_split_done"] = [(tr.get("split") or {}).get("last_done"), (tr.get("split") or {}).get("last_done_date")]
    ctx["sleep_debt_now"] = _hm(((m.get("sleep") or {}).get("tonight") or {}).get("debt"))
    return ctx


def parse_insights(raw: str | None) -> dict | None:
    """The {"today": …, "week": …} object out of Claude's reply, cleaned; None if it isn't there."""
    if not raw:
        return None
    a, b = raw.find("{"), raw.rfind("}")
    try:
        data = json.loads(raw[a:b + 1]) if 0 <= a < b else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for part in ("today", "week"):
        sec = data.get(part)
        if not isinstance(sec, dict):
            return None
        items = []
        for it in sec.get("insights") or []:
            if not isinstance(it, dict) or not str(it.get("finding") or "").strip():
                continue
            lvl = it.get("level") if it.get("level") in INSIGHT_LEVELS else "watch"
            items.append({"level": lvl, "finding": " ".join(str(it["finding"]).split()),
                          "action": " ".join(str(it.get("action") or "").split())})
        if not items:
            return None
        out[part] = {"headline": " ".join(str(sec.get("headline") or "").split()), "insights": items[:6]}
    return out


def generate_insights(ctx: dict, timeout: float = 240) -> dict | None:
    raw = ask(INSIGHTS_SYSTEM, "Today's and this week's data (JSON):\n" + json.dumps(ctx, default=str), timeout)
    return parse_insights(raw)


# Fixed next steps for the rule-based insights: (when it got better, when it got worse), by metric
# family. Claude's insights are specific to the numbers; these only have to be safe and concrete.
FALLBACK_ACTIONS = {
    "recovery": ("Keep sleep and training as they are this week.",
                 "Plan two easy days and protect a full night before each hard session."),
    "sleep": ("Keep the same lights-out time next week.",
              "Move lights-out 30 minutes earlier on at least four nights next week."),
    "heart": ("Keep the current balance of hard and easy days.",
              "Swap one hard session for an easy one and cut late meals and alcohol for a few days."),
    "activity": ("Keep the daily movement habit going.",
                 "Add a 20-minute walk on the days you don't train."),
    "lifting": ("Keep the split on schedule.", "Book your lifting sessions in the calendar for next week."),
}
FALLBACK_FAMILY = {"Recovery %": "recovery", "Sleep score": "sleep", "Sleep perf %": "sleep", "Asleep h": "sleep",
                   "HRV ms": "heart", "Resting HR": "heart", "Resp. rate": "heart", "Steps": "activity",
                   "Active kcal": "activity", "AZM": "activity", "Lifting min": "lifting"}
def attention_action(text: str, m: dict) -> str:
    """A next step for one Needs-attention item. Keyed on the fixed item formats in data.attention()
    and _verdict(), and on the model's own state where the right direction depends on it."""
    t = text.lower()
    if text == ((m.get("sleep") or {}).get("sync_note") or {}).get("text"):
        return "Open the Fitbit app on your phone, then run fitdash --sync."
    if t.startswith("recovery"):
        return "Keep today's training easy and aim for a full night."
    if t.startswith("sleep score") or t.startswith("sleep debt") or t.startswith("no sleep recorded"):
        return "Get to bed by tonight's asleep-by time."
    if t.startswith("load ratio"):
        zone = (((m.get("training") or {}).get("acwr")) or {}).get("zone")
        if zone == "low":
            return "Add one session this week and build back gradually."
        return "Keep the next two days easy; hold off on extra sessions until the ratio is back under 1.3."
    if "spike" in t:
        return "Swap the next run or game for lifting or a low-impact session."
    if t.startswith("bed and wake times"):
        return "Keep lights-out and wake-up within 30 minutes of the same time every day this week."
    if t.startswith("gym"):
        return "Put this week's remaining gym sessions in your calendar now."
    if t.startswith("weight below pace"):
        return "Add a 300-400 kcal snack with protein each day this week."
    if t.startswith("weight above pace"):
        return "Trim 200-300 kcal a day, mostly from snacks and drinks, and keep protein the same."
    if t.startswith("skin temp"):
        return "Take it easy today; if you feel unwell, rest and check again tomorrow."
    return "Check this again tomorrow morning."


def fallback_insights(m: dict, now: datetime) -> dict:
    """Rule-based insights from the same numbers, for when Claude isn't available. Same schema as
    Claude's (levels good / watch / flag, every insight with an action)."""
    today = []
    for lvl, t in (m.get("attention") or [])[:4]:
        today.append({"level": lvl if lvl in INSIGHT_LEVELS else "watch", "finding": t, "action": attention_action(t, m)})
    note = fallback(m, now)
    head = ((m.get("verdict") or {}).get("headline") or "").rstrip(".")
    if note and not (head and head in note):
        band = (m.get("recovery") or {}).get("band")
        today.insert(0, {"level": {"green": "good", "red": "flag"}.get(band, "watch"), "finding": note,
                         "action": "Follow today's plan in the Today box."})
    week = []
    for r in (m.get("week_review") or {}).get("rows") or []:
        if r.get("avg") is None or r.get("prev") in (None, 0) or r.get("higher_better") is None:
            continue
        ch = (r["avg"] - r["prev"]) / abs(r["prev"])
        if abs(ch) < 0.08:
            continue
        better = (ch > 0) == r["higher_better"]
        keep, fix = FALLBACK_ACTIONS.get(FALLBACK_FAMILY.get(r["metric"], ""), ("Keep it up.", "Watch this next week."))
        week.append((abs(ch), {"level": "good" if better else "watch",
                               "finding": "{} averaged {:.1f} this week vs {:.1f} the week before ({:+.0%}).".format(
                                   r["metric"], r["avg"], r["prev"], ch), "action": keep if better else fix}))
    week = [w for _, w in sorted(week, key=lambda x: -x[0])][:5]
    return {"today": {"headline": (m.get("verdict") or {}).get("headline") or "", "insights": today[:5]},
            "week": {"headline": "This week vs the week before", "insights": week or [
                {"level": "watch", "finding": "Not enough data for a week-on-week comparison yet.",
                 "action": "Wear the watch day and night so next week has a full comparison."}]}}


def insights_path(home: Path) -> Path:
    return home / "coaching" / "insights.json"


def save_insights(home: Path, result: dict) -> None:
    p = insights_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1) + "\n")
    tmp.replace(p)


def load_insights(home: Path) -> dict | None:
    try:
        data = json.loads(insights_path(home).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("today"), dict) else None


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
