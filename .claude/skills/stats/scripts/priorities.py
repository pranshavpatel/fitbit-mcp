"""Today's top 3 priorities: set them in the morning, review them in the evening.

    fitdash priorities                         set (morning) or review (evening), asking in the terminal
    fitdash priorities set "ship the report" "gym: pull day" "call mom"
    fitdash priorities done 1 | some 2 | missed 3     mark one: done, some progress, not today
    fitdash priorities reflect "what helped / what got in the way"
    fitdash priorities show [yesterday|YYYY-MM-DD]

`fitdash` itself asks for them the first time it runs in a terminal in the morning (Enter skips and
it won't ask again that day), and asks for a review in the evening. Set "priorities_prompt": false
in stats.json, or FITDASH_NO_PROMPT=1, to turn the prompts off. Until 04:00 it's still yesterday.
Stored in ~/.fitbit-mcp/priorities.json.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

MAX = 3
STATUSES = {"done": "done", "some": "partial", "partial": "partial", "progress": "partial",
            "missed": "missed", "skip": "missed", "not": "missed"}
LABEL = {"done": "done", "partial": "some progress", "missed": "not today", None: "open"}
GLYPH = {"done": "●", "partial": "◐", "missed": "✕", None: "○"}
MORNING = (4, 14)              # the morning prompt runs from 04:00 until 14:00
EVENING_FROM = 19              # the review prompt from 19:00 (and until 04:00, for the day before)
ROLLOVER_H = 4


class PriorityError(ValueError):
    pass


def path(home: Path) -> Path:
    return home / "priorities.json"


def load(home: Path) -> dict[str, dict]:
    try:
        data = json.loads(path(home).read_text())
    except (OSError, ValueError):
        return {}
    days = data.get("days") if isinstance(data, dict) else None
    out = {}
    for d, e in (days or {}).items():
        if not (isinstance(e, dict) and re.match(r"^\d{4}-\d{2}-\d{2}$", d)):
            continue
        items = [{"text": str(i.get("text", ""))[:200], "status": i.get("status") if i.get("status") in LABEL else None}
                 for i in e.get("items") or [] if isinstance(i, dict) and str(i.get("text", "")).strip()][:MAX]
        out[d] = {"items": items, "reflection": str(e.get("reflection") or "")[:2000],
                  "set_at": e.get("set_at"), "reviewed_at": e.get("reviewed_at"),
                  "prompt_skipped": bool(e.get("prompt_skipped")), "review_skipped": bool(e.get("review_skipped"))}
    return out


def save(home: Path, days: dict[str, dict]) -> None:
    p = path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "days": dict(sorted(days.items()))}, indent=1) + "\n")
    tmp.replace(p)


def plan_day(now: datetime) -> date:
    """The day priorities belong to: until 04:00 it's still the evening before."""
    return now.date() - timedelta(days=1) if now.hour < ROLLOVER_H else now.date()


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M")


def set_items(home: Path, day: date, texts: list[str], now: datetime) -> dict:
    texts = [" ".join(t.split()) for t in texts if t and t.strip()]
    if not texts:
        raise PriorityError("give at least one priority")
    if len(texts) > MAX:
        raise PriorityError("three priorities at most: what matters most today?")
    days = load(home)
    e = days.get(day.isoformat()) or {"items": [], "reflection": ""}
    old = {i["text"]: i["status"] for i in e.get("items", [])}
    e["items"] = [{"text": t[:200], "status": old.get(t[:200])} for t in texts]
    e["set_at"] = e.get("set_at") or _stamp(now)
    days[day.isoformat()] = e
    save(home, days)
    return e


def mark(home: Path, day: date, index: int, status: str | None, now: datetime) -> dict:
    days = load(home)
    e = days.get(day.isoformat())
    if not e or not e["items"]:
        raise PriorityError("no priorities set for {}".format(day.isoformat()))
    if not 1 <= index <= len(e["items"]):
        raise PriorityError("pick 1-{}".format(len(e["items"])))
    if status is not None and status not in LABEL:
        status = STATUSES.get(status)
        if status is None:
            raise PriorityError("status is done, some or missed")
    e["items"][index - 1]["status"] = status
    if all(i["status"] for i in e["items"]):
        e["reviewed_at"] = e.get("reviewed_at") or _stamp(now)
    save(home, days)
    return e


def reflect(home: Path, day: date, text: str, now: datetime) -> dict:
    days = load(home)
    e = days.get(day.isoformat()) or {"items": [], "reflection": ""}
    e["reflection"] = " ".join((text or "").split())[:2000]
    days[day.isoformat()] = e
    save(home, days)
    return e


def flag(home: Path, day: date, key: str) -> None:
    """Remember that a prompt was skipped today, so fitdash doesn't ask again."""
    days = load(home)
    e = days.get(day.isoformat()) or {"items": [], "reflection": ""}
    e[key] = True
    days[day.isoformat()] = e
    save(home, days)


def summary(days: dict[str, dict], day: date, n: int = 7) -> dict:
    """The last n days: how many had priorities set, items done / partly / missed, and the streak of
    days with priorities set (today counts once it has them)."""
    span = [(day - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]
    items = [i for d in span for i in (days.get(d) or {}).get("items", [])]
    set_days = [d for d in span if (days.get(d) or {}).get("items")]
    streak, x = 0, day if (days.get(day.isoformat()) or {}).get("items") else day - timedelta(days=1)
    while (days.get(x.isoformat()) or {}).get("items"):
        streak, x = streak + 1, x - timedelta(days=1)
    return {"days": n, "set_days": len(set_days), "items": len(items),
            "done": sum(1 for i in items if i["status"] == "done"),
            "partial": sum(1 for i in items if i["status"] == "partial"),
            "missed": sum(1 for i in items if i["status"] == "missed"),
            "open": sum(1 for i in items if i["status"] is None), "streak": streak,
            "by_day": [{"date": d, "items": (days.get(d) or {}).get("items", [])} for d in span]}


def describe(e: dict | None) -> str:
    if not e or not e.get("items"):
        return "no priorities set"
    lines = ["  {} {}. {}  ({})".format(GLYPH[i["status"]], k, i["text"], LABEL[i["status"]])
             for k, i in enumerate(e["items"], 1)]
    if e.get("reflection"):
        lines.append("  reflection: " + e["reflection"])
    return "\n".join(lines)


# ---------------------------------------------------------------- prompts

def ask_set(inp=input, out=sys.stdout) -> list[str]:
    out.write("What are your top 3 priorities today? (Enter on an empty line to stop)\n")
    texts = []
    for k in range(1, MAX + 1):
        t = inp("  {}. ".format(k)).strip()
        if not t:
            break
        texts.append(t)
    return texts


def ask_review(e: dict, inp=input, out=sys.stdout) -> tuple[list[str | None], str | None]:
    out.write("How did today's priorities go?  d = done · s = some progress · n = not today · Enter = skip\n")
    statuses: list[str | None] = []
    for k, i in enumerate(e["items"], 1):
        while True:
            a = inp("  {}. {}  [d/s/n] ".format(k, i["text"])).strip().lower()
            if a in ("", "d", "s", "n", "done", "some", "not"):
                break
        statuses.append({"d": "done", "done": "done", "s": "partial", "some": "partial", "n": "missed", "not": "missed"}.get(a, i["status"]))
    note = inp("  What helped, or what got in the way? (Enter to skip) ").strip()
    return statuses, note or None


def due_prompt(days: dict[str, dict], now: datetime) -> str | None:
    """'set' in the morning if today has none (and wasn't skipped), 'review' in the evening if
    today's are set but not all marked (and the review wasn't skipped), else None."""
    d = plan_day(now)
    e = days.get(d.isoformat()) or {}
    late = now.hour >= EVENING_FROM or now.hour < ROLLOVER_H
    if MORNING[0] <= now.hour < MORNING[1] and not e.get("items") and not e.get("prompt_skipped"):
        return "set"
    if late and e.get("items") and not all(i["status"] for i in e["items"]) and not e.get("review_skipped"):
        return "review"
    return None


def run_prompt(home: Path, now: datetime, kind: str, inp=input, out=sys.stdout) -> None:
    d = plan_day(now)
    try:
        if kind == "set":
            texts = ask_set(inp, out)
            if texts:
                set_items(home, d, texts, now)
                out.write("Saved. The coach will check in on them.\n\n")
            else:
                flag(home, d, "prompt_skipped")
                out.write("Skipped for today.\n\n")
        else:
            e = load(home)[d.isoformat()]
            statuses, note = ask_review(e, inp, out)
            if not any(statuses) and not note:
                flag(home, d, "review_skipped")
                out.write("Skipped.\n\n")
                return
            for k, st in enumerate(statuses, 1):
                if st:
                    mark(home, d, k, st, now)
            if note:
                reflect(home, d, note, now)
            out.write("Saved.\n\n")
    except (EOFError, KeyboardInterrupt):
        out.write("\n")


def prompts_enabled(cfg: dict) -> bool:
    return cfg.get("priorities_prompt", True) is not False and os.environ.get("FITDASH_NO_PROMPT") != "1"


# ---------------------------------------------------------------- CLI

def cli(argv: list[str], home: Path, now: datetime, interactive: bool) -> int:
    d = plan_day(now)
    if argv[:1] == ["show"]:
        arg = argv[1] if len(argv) > 1 else None
        if arg == "yesterday":
            d -= timedelta(days=1)
        elif arg:
            d = date.fromisoformat(arg)
        print("{}:\n{}".format(d.isoformat(), describe(load(home).get(d.isoformat()))))
        return 0
    try:
        if argv[:1] == ["set"]:
            e = set_items(home, d, argv[1:], now)
            print("priorities for {}:\n{}".format(d.isoformat(), describe(e)))
            return 0
        if argv and argv[0] in STATUSES:
            if len(argv) != 2 or not argv[1].isdigit():
                print("usage: fitdash priorities {} <1-3>".format(argv[0]), file=sys.stderr)
                return 2
            e = mark(home, d, int(argv[1]), STATUSES[argv[0]], now)
            print(describe(e))
            return 0
        if argv[:1] == ["reflect"]:
            reflect(home, d, " ".join(argv[1:]), now)
            print("saved")
            return 0
    except PriorityError as exc:
        print("fitdash priorities: {}".format(exc), file=sys.stderr)
        return 2
    if argv:
        print(__doc__)
        return 2
    days = load(home)
    e = days.get(d.isoformat())
    if not interactive:
        print("{}:\n{}".format(d.isoformat(), describe(e)))
        return 0
    if e and e.get("items") and (now.hour >= 12 or now.hour < ROLLOVER_H):
        run_prompt(home, now, "review", out=sys.stdout)
    else:
        run_prompt(home, now, "set", out=sys.stdout)
    print(describe(load(home).get(d.isoformat())))
    return 0
