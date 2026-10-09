"""The lift log: what you actually did in the gym, typed in one line.

    fitdash lift bench 3x8@60 row 4x10@50          today
    fitdash lift yesterday squat 5x5@100 rdl 3x8@80
    fitdash lift pullups 3x8 dips 3x12              bodyweight: no @
    fitdash lift ohp 1x5@40 2x8@35                  several set groups for one exercise
    fitdash lift curls 3x12@25lb                    pounds are converted to kg
    fitdash lift                                    show today's log
    fitdash lift undo                               remove today's last entry

Stored in ~/.fitbit-mcp/lift_log.json and nowhere else. Nothing here touches the Fitbit database
or the network. A set pattern is SETSxREPS, optionally @WEIGHT with kg (default) or lb.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import scores as S

LB_TO_KG = 0.45359237
SET_RE = re.compile(r"^(\d{1,2})x(\d{1,3})(?:@(\d+(?:\.\d+)?)(kg|kgs|lb|lbs)?)?$", re.I)


class LiftError(ValueError):
    pass


def log_path(home: Path) -> Path:
    return home / "lift_log.json"


def load(home: Path) -> dict[str, list[dict]]:
    """Date → entries ({"exercise", "sets", "reps", "kg" or None, "at"}). Missing or unreadable → {}."""
    try:
        data = json.loads(log_path(home).read_text())
    except (OSError, ValueError):
        return {}
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, dict):
        return {}
    out = {}
    for d, entries in days.items():
        good = [e for e in entries if isinstance(e, dict) and S.canonical_exercise(str(e.get("exercise", "")))
                and isinstance(e.get("sets"), int) and isinstance(e.get("reps"), int)]
        for e in good:
            e["exercise"] = S.canonical_exercise(e["exercise"])
        if good:
            out[d] = good
    return out


def save(home: Path, days: dict[str, list[dict]]) -> None:
    path = log_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "days": dict(sorted(days.items()))}, indent=1) + "\n")
    tmp.replace(path)


def parse(tokens: list[str], at: str) -> list[dict]:
    """Words build an exercise name; each set pattern after it adds a set group to that exercise."""
    entries, name, pending = [], [], False
    for tok in tokens:
        m = SET_RE.match(tok.strip(","))
        if not m:
            if pending:                  # a new exercise starts after the previous one's sets
                name, pending = [], False
            name.append(tok)
            continue
        if not name:
            raise LiftError("'{}' has no exercise before it".format(tok))
        ex = S.canonical_exercise(" ".join(name))
        if ex is None:
            raise LiftError("don't know the exercise '{}' (try e.g. bench, row, squat, rdl, ohp, curl)"
                            .format(" ".join(name)))
        sets, reps = int(m.group(1)), int(m.group(2))
        if not (1 <= sets <= 20 and 1 <= reps <= 100):
            raise LiftError("'{}' looks wrong: 1-20 sets of 1-100 reps".format(tok))
        kg = None
        if m.group(3):
            kg = float(m.group(3))
            if (m.group(4) or "").lower().startswith("lb"):
                kg *= LB_TO_KG
            kg = round(kg, 1)
        entries.append({"exercise": ex, "sets": sets, "reps": reps, "kg": kg, "at": at})
        pending = True
    if name and not pending:
        raise LiftError("'{}' has no sets (write e.g. 3x8@60)".format(" ".join(name)))
    if not entries:
        raise LiftError("nothing to log")
    return entries


def describe(e: dict) -> str:
    w = "" if e.get("kg") is None else "@{:g}".format(e["kg"])
    return "{} {}x{}{}".format(e["exercise"], e["sets"], e["reps"], w)


def cli(argv: list[str], home: Path, now: datetime) -> int:
    import sys
    today = now.date()
    day = today
    if argv and argv[0] in ("today", "yesterday"):
        day = today - timedelta(days=argv[0] == "yesterday")
        argv = argv[1:]
    elif argv and re.match(r"^\d{4}-\d{2}-\d{2}$", argv[0]):
        try:
            day = date.fromisoformat(argv[0])
        except ValueError:
            print("not a date: {}".format(argv[0]), file=sys.stderr)
            return 2
        argv = argv[1:]
    days = load(home)
    key = day.isoformat()
    if argv[:1] == ["undo"]:
        if not days.get(key):
            print("nothing logged on {}".format(key), file=sys.stderr)
            return 1
        gone = days[key].pop()
        if not days[key]:
            del days[key]
        save(home, days)
        print("removed {} from {}".format(describe(gone), key))
        return 0
    if not argv:
        entries = days.get(key) or []
        print("{}: {}".format(key, "; ".join(describe(e) for e in entries) if entries else "nothing logged"))
        return 0
    at = now.strftime("%Y-%m-%dT%H:%M") if day == today else key + "T18:00"
    try:
        new = parse(argv, at)
    except LiftError as exc:
        print("fitdash lift: {}".format(exc), file=sys.stderr)
        return 2
    days.setdefault(key, []).extend(new)
    save(home, days)
    sets = S.weekly_sets(new)
    worked = ", ".join("{} {:g}".format(mu, s) for mu, s in sorted(sets.items(), key=lambda kv: -kv[1]) if s)
    print("logged {}: {}".format(key, "; ".join(describe(e) for e in new)))
    print("  sets → {}".format(worked))
    return 0
