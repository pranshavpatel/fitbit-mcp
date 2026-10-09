#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13"]
# ///
"""Regenerate the screenshots in docs/demo from SYNTHETIC data.

    uv run --script docs/demo/make_demos.py            # terminal SVGs (phone app: make_app_demos.py)

Nothing here reads your own ~/.fitbit-mcp: it builds a throwaway data home with the test suite's
synthetic Fitbit history (fixture_db.py), a made-up lift log, journal and coach note, then renders
each view with the real dashboard code at a fixed "now".
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SKILL = REPO / ".claude" / "skills" / "stats"
HOME = Path(tempfile.mkdtemp(prefix="fitdash-demo-"))
os.environ["FITBIT_MCP_HOME"] = str(HOME)
os.environ["FITDASH_TZ"] = "America/New_York"
sys.path[:0] = [str(SKILL / "scripts"), str(SKILL / "tests")]

import fixture_db  # noqa: E402
import coach as CO  # noqa: E402
import dashboard  # noqa: E402
import data as D  # noqa: E402
import journal as J  # noqa: E402
import lifts as L  # noqa: E402
import web as W  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.terminal_theme import TerminalTheme  # noqa: E402

DAY = fixture_db.FIXTURE_DAY
NOW = datetime(DAY.year, DAY.month, DAY.day, 11, 20, tzinfo=D.NY)    # just after the fixture's sync
THEME = TerminalTheme((17, 17, 17), (236, 235, 230), [(0, 0, 0)] * 8, [(128, 128, 128)] * 8)

LIFTS = {
    -16: "bench 4x8@57.5 incline db press 3x10@20 dips 3x12 pushdown 3x12@25",
    -14: "row 4x10@47.5 pulldown 3x10@50 curls 3x12@12",
    -12: "squat 4x6@90 rdl 3x8@70 calf raise 4x15@60 crunch 3x15",
    -9: "bench 4x8@60 incline db press 3x10@22 dips 3x12 pushdown 3x12@27.5",
    -7: "row 4x10@50 pulldown 3x10@52.5 curls 3x12@12 face pull 3x15@15",
    -5: "squat 4x6@95 rdl 3x8@75 leg curl 3x12@35 calf raise 4x15@60",
    -3: "bench 4x8@62.5 incline db press 3x10@22 ohp 3x8@37.5 pushdown 3x12@27.5 lateral raise 3x15@8",
    -1: "row 4x10@52.5 pullups 4x8 curls 3x12@14 hammer curl 3x12@12",
}
COACH = ("Recovery is {band} at {rec}%: HRV is {hrv_gap} ms under your average after {asleep} asleep, and "
         "you're carrying {debt} of sleep debt. {split} is {fresh}% fresh, so lift as planned but cap strain "
         "near 12, no extra conditioning. The bigger win is tonight: asleep by {by} to start paying the debt back.")


def make_home() -> None:
    fixture_db.create(HOME / "fitbit.sqlite3")
    (HOME / "coaching").mkdir(exist_ok=True)
    cfg = dict(fixture_db.CONFIG, timezone="America/New_York",
               experiment={"name": "Fixed 8:30 wake, lights out 12:45", "start": "2026-10-01", "nights": 14,
                           "lights_out": "00:45"},
               habits=[{"key": "alcohol", "label": "alcohol", "good": False},
                       {"key": "late-caffeine", "label": "caffeine after 2pm", "good": False},
                       {"key": "screens", "label": "screens in bed", "good": False},
                       {"key": "junk-food", "label": "junk food", "good": False},
                       {"key": "mobility", "label": "mobility", "good": True},
                       {"key": "creatine", "label": "creatine", "good": True},
                       {"key": "protein", "label": "hit protein goal", "good": True},
                       {"key": "read", "label": "read 20 min", "good": True}])
    (HOME / "coaching" / "stats.json").write_text(json.dumps(cfg, indent=1))
    L.save(HOME, {(DAY + timedelta(days=k)).isoformat(): L.parse(t.split(), (DAY + timedelta(days=k)).isoformat() + "T19:00")
                  for k, t in LIFTS.items()})
    rnd = random.Random(11)
    jd = {}
    for i in range(1, 50):
        d = (DAY - timedelta(days=i)).isoformat()
        if rnd.random() < 0.85:
            jd[d] = {"habits": {h["key"]: rnd.random() < (0.65 if h["good"] else 0.25) for h in cfg["habits"]},
                     "note": ""}
    jd[(DAY - timedelta(days=1)).isoformat()]["note"] = "Good pull session, slept with the phone outside the room."
    J.save(HOME, jd)
    # the sample coach note quotes the synthetic day's real numbers, so it can't drift from the rings
    m = model()
    sp, tn = m["training"]["split"], m["sleep"]["tonight"]
    hrv_gap = round(m["baselines"]["hrv"]["mean"] - m["series"]["hrv"][-1])
    by = int(tn["asleep_by"]) % 1440
    text = COACH.format(band=m["recovery"]["band"], rec=m["recovery"]["score"], hrv_gap=hrv_gap,
                        asleep=D._hm(m["sleep"]["asleep"]), debt=D._hm(tn["debt"]),
                        split=sp["next"][0].upper() + sp["next"][1:], fresh=sp["fresh"][sp["next"]],
                        by="{}:{:02d}".format((by // 60) % 12 or 12, by % 60) + (" am" if by < 720 else " pm"))
    CO.save(HOME, [{"date": DAY.isoformat(), "at": NOW.strftime("%Y-%m-%dT%H:%M"), "kind": "morning",
                    "text": text, "source": "claude"}])


def model() -> dict:
    store = D.open_store(HOME)
    try:
        return D.build_model(store, DAY, D.load_config(HOME), now=NOW)
    finally:
        store.close()


def svg(name: str, width: int, draw, title: str) -> None:
    con = Console(record=True, width=width, file=open(os.devnull, "w"), color_system="truecolor",
                  force_terminal=True, highlight=False, emoji=False, soft_wrap=False)
    draw(con)
    con.save_svg(str(HERE / (name + ".svg")), title=title, theme=THEME)
    print("wrote docs/demo/{}.svg".format(name))


def main() -> int:
    make_home()
    m = model()
    shots = [
        ("dashboard", 160, None, "fitdash"),
        ("today", 100, "today", "fitdash --section today"),
        ("freshness", 100, "freshness", "fitdash --section freshness"),
        ("lifting", 80, "lifting", "fitdash --section lifting"),
        ("insights", 80, "insights", "fitdash --section insights"),
        ("journal", 80, "journal", "fitdash --section journal"),
        ("sleep", 100, "sleep", "fitdash --section sleep"),
        ("training", 100, "training", "fitdash --section training"),
    ]
    gallery = {**m, "deep": None}                  # the gallery shows the normal boxes only
    for name, width, section, title in shots:
        svg(name, width, lambda con, w=width, s=section: dashboard.render(gallery, con, w, s), title)
    # extended single-section views (8 weeks of history + the extra boxes)
    store = D.open_store(HOME)
    try:
        m56 = D.build_model(store, DAY, D.load_config(HOME), days=56, now=NOW)
    finally:
        store.close()
    for section in ("sleep", "lifting"):
        svg(section + "_extended", 100, lambda con, s=section: dashboard.render(m56, con, 100, s),
            "fitdash --section " + section)
    shutil.rmtree(HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
