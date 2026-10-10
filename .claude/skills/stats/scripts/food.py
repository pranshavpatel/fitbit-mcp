"""A local food log: what you ate, with calories and macros (usually estimates).

    fitdash food add "southwest veggie wrap" 500 p=20 c=69 f=17 [meal=lunch] [at=12:30] [day=yesterday]
    fitdash food                       today's entries and totals
    fitdash food show [yesterday|YYYY-MM-DD]
    fitdash food undo [yesterday|YYYY-MM-DD]       remove the last entry of that day

The number after the name is kcal; p, c and f are grams of protein, carbs and fat. Entries are
stored in ~/.fitbit-mcp/food_log.json and nowhere else (nothing is written to Fitbit or Google).
Body & nutrition and the coach use them when Fitbit has no food logged for the day.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

MACROS = {"p": "protein", "c": "carbs", "f": "fat"}
MEALS = ("breakfast", "lunch", "dinner", "snack", "drink")


class FoodError(ValueError):
    pass


def path(home: Path) -> Path:
    return home / "food_log.json"


def load(home: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(path(home).read_text())
    except (OSError, ValueError):
        return {}
    days = data.get("days") if isinstance(data, dict) else None
    out = {}
    for d, entries in (days or {}).items():
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(d)) or not isinstance(entries, list):
            continue
        good = [e for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)
                and isinstance(e.get("kcal"), (int, float))]
        if good:
            out[d] = good
    return out


def save(home: Path, days: dict[str, list[dict]]) -> None:
    p = path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "days": dict(sorted(days.items()))}, indent=1) + "\n")
    tmp.replace(p)


def totals(entries: list[dict]) -> dict:
    t = {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
    for e in entries:
        for k in t:
            t[k] += e.get(k) or 0
    return {k: round(v, 1) for k, v in t.items()}


def parse_add(args: list[str], now: datetime) -> tuple[date, dict]:
    """name kcal [p=] [c=] [f=] [meal=] [at=HH:MM] [day=today|yesterday|YYYY-MM-DD]"""
    if len(args) < 2:
        raise FoodError('usage: fitdash food add "<what>" <kcal> [p=grams c=grams f=grams] [meal=lunch] [at=12:30]')
    name, rest = args[0].strip(), args[1:]
    if not name:
        raise FoodError("give the food a name")
    try:
        kcal = float(rest[0])
    except ValueError:
        raise FoodError("the number after the name is kcal, e.g. 500")
    if not 0 <= kcal <= 5000:
        raise FoodError("kcal should be between 0 and 5000")
    entry = {"name": name[:120], "kcal": round(kcal)}
    day = now.date()
    for tok in rest[1:]:
        key, _, val = tok.partition("=")
        key = key.lower()
        if key in MACROS:
            try:
                g = float(val)
            except ValueError:
                raise FoodError("{}= takes grams, e.g. p=20".format(key))
            if not 0 <= g <= 500:
                raise FoodError("{} should be 0-500 g".format(MACROS[key]))
            entry[MACROS[key]] = round(g, 1)
        elif key == "meal":
            if val not in MEALS:
                raise FoodError("meal is one of " + ", ".join(MEALS))
            entry["meal"] = val
        elif key == "at":
            if not re.match(r"^\d{1,2}:\d{2}$", val):
                raise FoodError("at= takes a time like 12:30")
            entry["at"] = val.zfill(5)
        elif key == "day":
            day = now.date() - timedelta(days=1) if val == "yesterday" else now.date() if val == "today" else date.fromisoformat(val)
        else:
            raise FoodError("unknown option '{}': use p= c= f= meal= at= day=".format(tok))
    entry.setdefault("at", now.strftime("%H:%M") if day == now.date() else "")
    entry["estimated"] = True
    return day, entry


def describe(entries: list[dict]) -> str:
    if not entries:
        return "  nothing logged"
    lines = []
    for e in entries:
        macros = " · ".join("{} {:g}g".format(k[0].upper(), e[k]) for k in ("protein", "carbs", "fat") if e.get(k) is not None)
        lines.append("  {:<5} {:<38} {:>5} kcal  {}".format(e.get("at") or "", e["name"][:38], e["kcal"], macros))
    t = totals(entries)
    lines.append("  total {:>44} kcal  P {:g}g · C {:g}g · F {:g}g".format(round(t["kcal"]), t["protein"], t["carbs"], t["fat"]))
    return "\n".join(lines)


def _day(arg: str | None, now: datetime) -> date:
    if not arg or arg == "today":
        return now.date()
    if arg == "yesterday":
        return now.date() - timedelta(days=1)
    return date.fromisoformat(arg)


def cli(argv: list[str], home: Path, now: datetime) -> int:
    try:
        if argv[:1] == ["add"]:
            day, entry = parse_add(argv[1:], now)
            days = load(home)
            days.setdefault(day.isoformat(), []).append(entry)
            save(home, days)
            print("logged {}: {} ({} kcal)".format(day.isoformat(), entry["name"], entry["kcal"]))
            return 0
        if argv[:1] == ["undo"]:
            day = _day(argv[1] if len(argv) > 1 else None, now)
            days = load(home)
            if not days.get(day.isoformat()):
                print("nothing logged on {}".format(day.isoformat()), file=sys.stderr)
                return 1
            gone = days[day.isoformat()].pop()
            if not days[day.isoformat()]:
                del days[day.isoformat()]
            save(home, days)
            print("removed {} from {}".format(gone["name"], day.isoformat()))
            return 0
        if not argv or argv[0] == "show":
            day = _day(argv[1] if len(argv) > 1 else None, now)
            print("{}:\n{}".format(day.isoformat(), describe(load(home).get(day.isoformat(), []))))
            return 0
    except (FoodError, ValueError) as exc:
        print("fitdash food: {}".format(exc), file=sys.stderr)
        return 2
    print(__doc__)
    return 2
