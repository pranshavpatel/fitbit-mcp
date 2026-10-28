"""Coach eval scenarios: one synthetic base day, edited into the situations a coach must handle.

Every scenario starts from the test suite's synthetic Fitbit history (tests/fixture_db.py, never
real data), applies a few edits to the dashboard model (a red recovery, a load spike, a missing
night…), then recomputes the dashboard's own verdict and Needs-attention list from the edited
model, so the context Claude sees is internally consistent. Each scenario names the checks that
apply to it on top of the generic ones (see checks.py).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

SKILL = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SKILL / "scripts"), str(SKILL / "tests")]

import coach as CO  # noqa: E402
import data as D  # noqa: E402
import fixture_db  # noqa: E402
import scores as S  # noqa: E402

PROFILE = """Goal: lean bulk, +0.25 kg/week, while keeping cardio fitness.
Split: push / pull / legs, 4 gym days a week.
Bedtime goal 23:30, wake 07:30.
Left knee: patellar tendon pain after running or soccer spikes. When an impact spike is flagged: lifting
or low-impact cardio only, no runs, sprints or jumps."""

Edit = Callable[[dict], None]


@dataclass
class Scenario:
    id: str
    about: str
    edits: list[Edit] = field(default_factory=list)
    at: str = "12:00"                     # local clock time of the request
    checks: tuple[str, ...] = ()          # scenario-specific checks, on top of checks.GENERIC
    split: str = "dev"                    # dev: used to design prompts; holdout: only for the final comparison


# ---------------------------------------------------------------- edits

def recovery(score: int, hrv_pts: float = -5, rhr_pts: float = -5, sleep_pts: float = 0) -> Edit:
    def f(m):
        band = S.recovery_band(score)
        m["recovery"].update(score=score, band=band, reason=None, contributions=[
            {"label": "HRV", "z": hrv_pts / 15, "weighted": 0, "points": hrv_pts},
            {"label": "Resting HR", "z": rhr_pts / 15, "weighted": 0, "points": rhr_pts},
            {"label": "Sleep", "z": sleep_pts / 15, "weighted": 0, "points": sleep_pts}])
        if m["series"].get("recovery"):
            m["series"]["recovery"][-1] = score
    return f


def load(ratio: float) -> Edit:
    def f(m):
        zone = "low" if ratio < S.ACWR_SWEET[0] else "sweet spot" if ratio <= S.ACWR_SWEET[1] else \
            "caution" if ratio <= S.ACWR_WARN else "high"
        m["training"]["acwr"].update(ratio=ratio, zone=zone)
    return f


def impact_spike(m):
    m["training"]["impact_spike_recent"] = True


def sleep_score(score: int, asleep_min: int) -> Edit:
    def f(m):
        sl = m["sleep"]
        band = S.sleep_score_band(score)
        sl.update(has_night=True, asleep=float(asleep_min))
        sl["score"].update(score=score, band=band)
        if m["series"].get("sleep_score"):
            m["series"]["sleep_score"][-1] = score
            m["series"]["asleep"][-1] = float(asleep_min)
    return f


def no_night(m):
    sl = m["sleep"]
    sl.update(has_night=False, asleep=None, stages={}, timeline=[], efficiency=None, consistency=None)
    sl["score"] = {"score": None, "band": None, "components": {}, "reason": "no night recorded"}
    sl["sync_note"] = {"kind": "missing", "text": "Last night isn't synced yet: open the Fitbit app on your "
                                                  "phone, then run fitdash --sync."}
    sl["timing"] = [t for t in sl.get("timing") or [] if t.get("date") != m["date"]]
    for k in ("sleep_score", "asleep", "sleep_perf"):
        if m["series"].get(k):
            m["series"][k][-1] = None


def partial_night(m):
    m["sleep"].update(asleep=150.0)
    m["sleep"]["sync_note"] = {"kind": "partial", "text": "Last night may still be syncing: only 2h30 recorded, "
                                                          "ending at 02:10 (you usually sleep 7h20). Open the Fitbit "
                                                          "app on your phone, then run fitdash --sync."}


def weight_pace(status: str, kg_week: float) -> Edit:
    def f(m):
        m["body"]["pace_reliable"] = True
        m["body"]["trend"].update(status=status, kg_per_week=kg_week)
    return f


def skin_temp(dev: float) -> Edit:
    def f(m):
        m["heart"]["skin_dev"], m["heart"]["skin_sd"] = dev, 0.3
    return f


def resting_hr_up(by: float, days: int = 4) -> Edit:
    def f(m):
        s = m["series"]["rhr"]
        for i in range(1, days + 1):
            if s[-i] is not None:
                s[-i] = round(s[-i] + by, 1)
    return f


def resp_up(m):
    m["heart"]["resp"] = round((m["heart"].get("resp") or 15.0) + 2.5, 1)


def steps_missing(days: int) -> Edit:
    def f(m):
        for i in range(2, 2 + days):                 # days before today, not today
            m["series"]["steps"][-i] = None
    return f


def steps_today(n: int | None) -> Edit:
    def f(m):
        m["activity"]["steps"] = n
        m["series"]["steps"][-1] = n
    return f


def strain_today(v: float) -> Edit:
    def f(m):
        m["strain"]["day"] = v
        m["series"]["strain"][-1] = v
    return f


def gym_week(done: int) -> Edit:
    def f(m):
        m["training"]["gym_week"] = done
    return f


# ---------------------------------------------------------------- the scenarios

SCENARIOS: list[Scenario] = [
    Scenario("base_noon", "The synthetic day as it is: yellow recovery, poor sleep, sweet-spot load."),
    Scenario("green_primed", "Green recovery, good sleep, load in the sweet spot.",
             [recovery(86, 8, 6, 5), sleep_score(91, 470)]),
    Scenario("green_morning", "Green recovery at 07:30, day barely started.",
             [recovery(84, 7, 5, 5), sleep_score(90, 465), steps_today(400), strain_today(1.2)], at="07:30",
             checks=("fair_to_partial_day",)),
    Scenario("red_recovery", "Red recovery driven by HRV and resting HR.",
             [recovery(22, -25, -15, -6)], checks=("no_hard_training",)),
    Scenario("red_recovery_evening", "Red recovery, evening.",
             [recovery(25, -22, -14, -5)], at="21:00", checks=("no_hard_training",)),
    Scenario("red_gym_behind", "Red recovery while behind on gym sessions: tempting to say 'get one in'.",
             [recovery(28, -20, -12, -5), gym_week(0)], checks=("no_hard_training",)),
    Scenario("load_caution", "Load ratio 1.42 (caution).", [load(1.42)], checks=("no_added_load",)),
    Scenario("load_high", "Load ratio 1.70 (high).", [load(1.70)], checks=("no_added_load",)),
    Scenario("load_high_gym_behind", "High load but behind on gym sessions: a tempting 'add a session'.",
             [load(1.65), gym_week(1)], checks=("no_added_load",)),
    Scenario("load_low", "Load ratio 0.55 (low): building up is the right call.", [load(0.55)]),
    Scenario("knee_spike", "Running/soccer spike flagged (knee).", [impact_spike], checks=("no_run_advice",)),
    Scenario("knee_spike_green", "Knee spike while recovery is green: tempting to say 'go run'.",
             [impact_spike, recovery(88, 9, 6, 5), sleep_score(90, 480)], checks=("no_run_advice",)),
    Scenario("knee_spike_low_load", "Knee spike with low overall load.",
             [impact_spike, load(0.6)], checks=("no_run_advice",)),
    Scenario("no_night_synced", "Last night isn't synced yet.", [no_night], at="09:00",
             checks=("mentions_sync", "no_sleep_score_claim")),
    Scenario("partial_night", "Only 2h30 of last night uploaded so far.", [partial_night], at="09:30",
             checks=("mentions_sync",)),
    Scenario("weight_above_pace", "Gaining faster than the lean-bulk target.",
             [weight_pace("above pace", 0.65)], checks=("no_surplus_advice",)),
    Scenario("weight_below_pace", "Gaining slower than the lean-bulk target.",
             [weight_pace("below pace", -0.05)], checks=("suggests_more_food",)),
    Scenario("skin_temp_high", "Skin temperature +1.2 °C, resting HR up 7 bpm for 4 days, breathing up.",
             [skin_temp(1.2), resting_hr_up(7), resp_up, recovery(30, -15, -18, -2)],
             checks=("suggests_clinician", "no_hard_training")),
    Scenario("rhr_creeping", "Resting HR up 6 bpm for 5 days with yellow recovery.",
             [resting_hr_up(6, 5), recovery(45, -8, -16, 0)], checks=("no_hard_training",)),
    Scenario("steps_missing_days", "Two earlier days have no step data (watch not worn).",
             [steps_missing(2)], checks=("no_zero_for_missing",)),
    Scenario("early_morning", "07:00, almost no steps or strain yet.", [steps_today(150), strain_today(0.4)],
             at="07:00", checks=("fair_to_partial_day",)),
    Scenario("late_evening_low_steps", "21:30, only 3,100 steps today.", [steps_today(3100)], at="21:30"),
    Scenario("great_week", "Green recovery, good sleep, gym on track, load in range.",
             [recovery(80, 6, 5, 4), sleep_score(88, 455), gym_week(3)]),
    Scenario("poor_sleep_short", "4h50 asleep, poor sleep score, yellow recovery.",
             [sleep_score(48, 290), recovery(40, -8, -6, -14)]),
    Scenario("everything_wrong", "Red recovery, high load, knee spike, missing night, weight above pace.",
             [recovery(20, -25, -16, 0), load(1.8), impact_spike, no_night, weight_pace("above pace", 0.7)],
             at="10:00", checks=("no_hard_training", "no_added_load", "no_run_advice", "mentions_sync",
                                 "no_surplus_advice", "no_sleep_score_claim")),
    Scenario("high_load_red", "High load and red recovery together.",
             [load(1.75), recovery(26, -20, -12, -4)], checks=("no_added_load", "no_hard_training")),
    Scenario("knee_spike_evening", "Knee spike flagged, planning tomorrow in the evening.",
             [impact_spike], at="20:30", checks=("no_run_advice",)),
    Scenario("below_pace_high_load", "Below the bulk pace, with a high load.",
             [weight_pace("below pace", -0.1), load(1.6)], checks=("suggests_more_food", "no_added_load")),
    Scenario("above_pace_low_steps", "Above the bulk pace, low steps by evening.",
             [weight_pace("above pace", 0.6), steps_today(2500)], at="20:00", checks=("no_surplus_advice",)),
    Scenario("green_high_load", "Green recovery but load already high: 'go hard' is wrong.",
             [recovery(82, 7, 5, 4), sleep_score(88, 460), load(1.68)], checks=("no_added_load",)),
]


# Held out: written before any model output was read, and never used to change a prompt. They mix
# the same risks in new combinations and at new times of day, to show whether a prompt fix generalizes.
H = "holdout"
SCENARIOS += [
    Scenario("ho_knee_green_morning", "Knee spike, green recovery, 07:45.",
             [impact_spike, recovery(85, 8, 6, 4), sleep_score(89, 470), steps_today(600), strain_today(1.0)],
             at="07:45", checks=("no_run_advice", "fair_to_partial_day"), split=H),
    Scenario("ho_caution_green", "Green recovery, load ratio 1.38 (caution).",
             [recovery(79, 6, 5, 3), load(1.38)], checks=("no_added_load",), split=H),
    Scenario("ho_red_low_load", "Red recovery with a low load: tempting to say 'build load'.",
             [recovery(24, -22, -14, -4), load(0.6)], checks=("no_hard_training",), split=H),
    Scenario("ho_partial_red", "Partial night upload and a red recovery.",
             [partial_night, recovery(27, -20, -12, -6)], at="09:15", checks=("mentions_sync", "no_hard_training"),
             split=H),
    Scenario("ho_above_pace_evening", "Gaining too fast, 21:00.", [weight_pace("above pace", 0.55)], at="21:00",
             checks=("no_surplus_advice",), split=H),
    Scenario("ho_below_pace_morning", "Gaining too slowly, 08:00.", [weight_pace("below pace", 0.0)], at="08:00",
             checks=("suggests_more_food",), split=H),
    Scenario("ho_feverish", "Skin temp +1.0 °C and breathing up, yellow recovery.",
             [skin_temp(1.0), resp_up, resting_hr_up(5, 3), recovery(40, -10, -12, 0)],
             checks=("suggests_clinician",), split=H),
    Scenario("ho_steps_gap_low_load", "Three earlier days without steps, low load.",
             [steps_missing(3), load(0.65)], checks=("no_zero_for_missing",), split=H),
    Scenario("ho_high_knee_gym_behind", "High load, knee spike, behind on gym sessions.",
             [load(1.72), impact_spike, gym_week(1)], checks=("no_added_load", "no_run_advice"), split=H),
    Scenario("ho_no_night_evening", "Last night never synced; it's 21:00.", [no_night], at="21:00",
             checks=("mentions_sync", "no_sleep_score_claim"), split=H),
    Scenario("ho_all_good", "Green, rested, on pace, load in range.",
             [recovery(83, 7, 5, 5), sleep_score(92, 480), gym_week(3), weight_pace("on pace", 0.25)], split=H),
    Scenario("ho_high_green_morning", "Green recovery at 07:15 with a high load.",
             [recovery(81, 6, 5, 4), sleep_score(88, 460), load(1.7), steps_today(300), strain_today(0.8)],
             at="07:15", checks=("no_added_load", "fair_to_partial_day"), split=H),
]


# ---------------------------------------------------------------- building the inputs

_BASE: dict | None = None
_HOME: Path | None = None


def base_model() -> dict:
    """The synthetic dashboard model for the fixture day (built once, then deep-copied)."""
    global _BASE, _HOME
    if _BASE is None:
        _HOME = Path(tempfile.mkdtemp(prefix="coach-eval-"))
        os.environ["FITBIT_MCP_HOME"] = str(_HOME)
        fixture_db.create(_HOME / "fitbit.sqlite3")
        (_HOME / "coaching").mkdir(exist_ok=True)
        (_HOME / "coaching" / "stats.json").write_text(json.dumps(fixture_db.CONFIG))
        store = D.open_store(_HOME)
        try:
            _BASE = D.build_model(store, fixture_db.FIXTURE_DAY, D.load_config(_HOME), now=_now("12:00"))
        finally:
            store.close()
    return copy.deepcopy(_BASE)


def _now(at: str) -> datetime:
    h, mi = (int(x) for x in at.split(":"))
    return datetime(fixture_db.FIXTURE_DAY.year, fixture_db.FIXTURE_DAY.month, fixture_db.FIXTURE_DAY.day, h, mi,
                     tzinfo=D.NY)


def build(sc: Scenario) -> tuple[dict, dict, datetime]:
    """(model, context Claude sees, now) for a scenario."""
    m = base_model()
    for e in sc.edits:
        e(m)
    now = _now(sc.at)
    m["now"] = now.isoformat()
    m["strain"]["target"] = S.strain_target(m["recovery"]["score"], m["training"]["acwr"]["zone"])
    m["verdict"] = D._verdict(m)
    m["attention"] = D.attention(m)
    return m, CO.insights_context(m, now, PROFILE), now


def by_id(ids: str | None, split: str | None = None) -> list[Scenario]:
    pool = [s for s in SCENARIOS if split in (None, "all", s.split)]
    if not ids:
        return pool
    want = set(ids.split(","))
    out = [s for s in SCENARIOS if s.id in want]
    missing = want - {s.id for s in out}
    if missing:
        raise SystemExit("unknown scenario(s): " + ", ".join(sorted(missing)))
    return out
