"""The daily journal: a few yes/no habits and an optional note, about one day.

    fitdash journal                        ask each habit (in a terminal), then a note
    fitdash journal +alcohol -screens +stretch
    fitdash journal yesterday +late-meal note "slept badly, hot room"
    fitdash journal 2026-10-07 -alcohol
    fitdash journal show [day]             what's logged
    fitdash journal habits                 the habit list and keys

+key = did it, -key = didn't. Keys match by prefix (+alc is enough). An entry is about the day the
habit happened; in the morning (before noon) the default day is yesterday, like a WHOOP journal.
Habits live in ~/.fitbit-mcp/coaching/stats.json ("habits"); entries in ~/.fitbit-mcp/journal.json.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# good: True = a healthy habit (doing it is the goal), False = one to avoid, None = neutral (just track it)
DEFAULT_HABITS = [
    {"key": "alcohol", "label": "alcohol", "good": False},
    {"key": "late-caffeine", "label": "caffeine after 2pm", "good": False},
    {"key": "late-meal", "label": "ate within 2 h of bed", "good": False},
    {"key": "screens", "label": "screens in bed", "good": False},
    {"key": "stretch", "label": "stretching / mobility", "good": True},
    {"key": "protein", "label": "hit protein goal", "good": True},
    {"key": "creatine", "label": "creatine", "good": None},
    {"key": "knee-pain", "label": "knee pain", "good": False},
]


GROUPS = ((True, "To do", "32"), (False, "To avoid", "31"), (None, "Tracking", "34"))   # ANSI green / red / blue


def _paint(text: str, code: str, out=None) -> str:
    import os
    out = out or sys.stdout
    on = hasattr(out, "isatty") and out.isatty() and "NO_COLOR" not in os.environ
    return "\033[1;{}m{}\033[0m".format(code, text) if on else text


def grouped(hs: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Habits to do first, then those to avoid, then neutral ones; empty groups left out."""
    return [(title, code, [h for h in hs if h.get("good") is good]) for good, title, code in GROUPS
            if any(h.get("good") is good for h in hs)]


class JournalError(ValueError):
    pass


def habits(cfg: dict) -> list[dict]:
    hs = cfg.get("habits")
    if not isinstance(hs, list) or not hs:
        return DEFAULT_HABITS
    out = []
    for h in hs:
        if isinstance(h, str):
            h = {"key": h, "label": h.replace("-", " "), "good": None}
        if isinstance(h, dict) and h.get("key"):
            out.append({"key": str(h["key"]), "label": str(h.get("label") or h["key"]), "good": h.get("good")})
    return out or DEFAULT_HABITS


def path(home: Path) -> Path:
    return home / "journal.json"


def load(home: Path) -> dict[str, dict]:
    """Date → {"habits": {key: bool}, "note": str}. Missing or unreadable → {}."""
    try:
        data = json.loads(path(home).read_text())
    except (OSError, ValueError):
        return {}
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, dict):
        return {}
    out = {}
    for d, e in days.items():
        if not (isinstance(e, dict) and re.match(r"^\d{4}-\d{2}-\d{2}$", d)):
            continue
        hb = {k: v for k, v in (e.get("habits") or {}).items() if isinstance(v, bool)}
        note = e.get("note") if isinstance(e.get("note"), str) else ""
        if hb or note:
            out[d] = {"habits": hb, "note": note}
    return out


def save(home: Path, days: dict[str, dict]) -> None:
    p = path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "days": dict(sorted(days.items()))}, indent=1) + "\n")
    tmp.replace(p)


def match(key: str, hs: list[dict]) -> str:
    k = key.lower().replace("_", "-")
    exact = [h["key"] for h in hs if h["key"] == k]
    if exact:
        return exact[0]
    found = [h["key"] for h in hs if h["key"].startswith(k)]
    if len(found) != 1:
        raise JournalError("'{}' matches {} habits; keys: {}".format(
            key, "no" if not found else len(found), ", ".join(h["key"] for h in hs)))
    return found[0]


def parse(tokens: list[str], hs: list[dict]) -> tuple[dict[str, bool], str | None]:
    """+key / -key marks; `note <text…>` (or any text after `note`) sets the note."""
    marks: dict[str, bool] = {}
    note = None
    for i, tok in enumerate(tokens):
        if tok == "note":
            note = " ".join(tokens[i + 1:]).strip()
            break
        if len(tok) > 1 and tok[0] in "+-":
            marks[match(tok[1:], hs)] = tok[0] == "+"
        else:
            raise JournalError("'{}': use +habit, -habit or note \"…\"".format(tok))
    return marks, note


def default_day(now: datetime) -> date:
    return now.date() - timedelta(days=1) if now.hour < 12 else now.date()


def describe(entry: dict, hs: list[dict]) -> str:
    lines = []
    for title, code, group in grouped(hs):
        parts = []
        for h in group:
            v = entry["habits"].get(h["key"])
            if v is not None:
                if h.get("good") is None:
                    parts.append(("yes " if v else "no ") + h["label"])
                    continue
                ok = v == h["good"]           # ✓ = a good day for this habit: did a "do", skipped an "avoid"
                parts.append(_paint(("✓ " if ok else "✗ ") + h["label"], "32" if ok else "31"))
        if parts:
            lines.append("  {:<9} {}".format(_paint(title, code), " · ".join(parts)))
    if entry.get("note"):
        lines.append("  note      " + entry["note"])
    return ("\n" + "\n".join(lines)) if lines else "no habits"


def ask(hs: list[dict], prev: dict, inp=input, out=sys.stdout) -> tuple[dict[str, bool], str | None]:
    """Interactive: y / n / Enter to skip (keeps what's already logged), then a note."""
    marks: dict[str, bool] = {}
    order = []
    for title, code, group in grouped(hs):
        order.append((title, code, None))
        order += [(None, None, h) for h in group]
    for title, code, h in order:
        if h is None:
            out.write(_paint(title.upper(), code, out) + "\n")
            continue
        cur = prev.get("habits", {}).get(h["key"])
        hint = "" if cur is None else " (now {})".format("yes" if cur else "no")
        while True:
            a = inp("  {}{}? [y/n/Enter skip] ".format(h["label"], hint)).strip().lower()
            if a in ("y", "yes"):
                marks[h["key"]] = True
            elif a in ("n", "no"):
                marks[h["key"]] = False
            elif a not in ("",):
                continue
            break
    note = inp("  note (Enter to skip): ").strip()
    return marks, note or None


def cli(argv: list[str], home: Path, cfg: dict, now: datetime, interactive: bool) -> int:
    hs = habits(cfg)
    if argv[:1] == ["habits"]:
        for title, code, group in grouped(hs):
            print(_paint(title.upper(), code))
            for h in group:
                print("  {:<14} {}".format(h["key"], h["label"]))
        print("edit the list under \"habits\" in ~/.fitbit-mcp/coaching/stats.json")
        return 0
    show = argv[:1] == ["show"]
    if show:
        argv = argv[1:]
    day = default_day(now)
    if argv and argv[0] in ("today", "yesterday"):
        day = now.date() - timedelta(days=argv[0] == "yesterday")
        argv = argv[1:]
    elif argv and re.match(r"^\d{4}-\d{2}-\d{2}$", argv[0]):
        try:
            day = date.fromisoformat(argv[0])
        except ValueError:
            print("not a date: {}".format(argv[0]), file=sys.stderr)
            return 2
        argv = argv[1:]
    if day > now.date():
        print("can't journal a future day", file=sys.stderr)
        return 2
    days = load(home)
    key = day.isoformat()
    entry = days.get(key) or {"habits": {}, "note": ""}
    if show or (not argv and not interactive):
        print("{}: {}".format(key, describe(entry, hs) if key in days else "nothing logged"))
        return 0
    if not argv:
        print("Journal for {} ({})".format(key, "yesterday" if day < now.date() else "today"))
        marks, note = ask(hs, entry)
    else:
        try:
            marks, note = parse(argv, hs)
        except JournalError as exc:
            print("fitdash journal: {}".format(exc), file=sys.stderr)
            return 2
    entry["habits"].update(marks)
    if note is not None:
        entry["note"] = note
    if not entry["habits"] and not entry["note"]:
        print("nothing to log")
        return 0
    days[key] = entry
    save(home, days)
    print("journal {}: {}".format(key, describe(entry, hs)))
    return 0
