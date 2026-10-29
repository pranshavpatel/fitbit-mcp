"""Workout-review eval scenarios (fitdash insights --workout).

Each scenario is a synthetic lift log (and, for one, only a Fitbit session) on top of the same synthetic
dashboard model as the insights eval (scenarios.base_model), plus edits to today's state: a red
recovery, a high training load, a knee flag. The context Claude sees is built by the same
coach.workout_context the command uses. `expect` is the set of ratings an honest reviewer could give;
the holdout scenarios (ho_*) were written before any model output was read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import scenarios as SC
from scenarios import CO, D, Edit, PROFILE, impact_spike, load, recovery, sleep_score

import lifts as L  # noqa: E402  (scenarios put scripts/ on the path)

WORKOUT_DAY = "2026-10-07"       # the day before the fixture day: "yesterday's" session

# the weeks before: one push, pull and legs day each
HISTORY = {
    "2026-09-30": "bench 3x8@60 incline db press 3x10@50lb lateral raise 3x12@8 pushdown 3x12@30",
    "2026-10-02": "row 3x10@50 lat pulldown 3x10@55 curl 3x12@12 pull up 3x6",
    "2026-10-03": "squat 4x6@90 rdl 3x8@80 leg press 3x10@140 calf raise 3x15@60",
}


@dataclass
class WorkoutScenario:
    id: str
    about: str
    session: str | None                       # the workout's sets (None: only a Fitbit session)
    expect: tuple[str, ...]                   # ratings an honest coach could give
    edits: list[Edit] = field(default_factory=list)
    history: dict[str, str] = field(default_factory=lambda: dict(HISTORY))
    profile: str = PROFILE
    at: str = "12:00"
    checks: tuple[str, ...] = ()
    split: str = "dev"


WORKOUT_SCENARIOS: list[WorkoutScenario] = [
    WorkoutScenario("pr_day", "push day, every lift up on last time",
                    "bench 1x8@62.5 2x8@60 incline db press 3x10@55lb lateral raise 3x12@9 pushdown 3x12@32",
                    ("great",), checks=("praises_progress",)),
    WorkoutScenario("tough_day", "push day well below last time after a short night, red recovery today",
                    "bench 1x8@60 1x6@60 1x4@60 incline db press 2x8@50lb pushdown 2x10@30",
                    ("mixed", "tough"), edits=[recovery(28, -14, -10, -8), sleep_score(52, 310)],
                    checks=("no_hard_today", "no_hype")),
    WorkoutScenario("first_time", "a session of exercises never logged before",
                    "dumbbell press 3x10@24 chest fly 3x12@10 dip 3x8", ("great", "good"),
                    checks=("no_false_comparison",)),
    WorkoutScenario("mixed_day", "bench PR, but incline press and pushdowns slipped",
                    "bench 3x8@62.5 incline db press 1x10@50lb 2x6@50lb pushdown 2x12@30 1x8@30",
                    ("mixed", "good"), checks=("no_hype",)),
    WorkoutScenario("knee_legs", "legs day with an impact spike and the knee note in the profile",
                    "leg press 3x10@150 leg curl 3x12@40 calf raise 3x15@60", ("great", "good"),
                    edits=[impact_spike], checks=("no_run_advice",)),
    WorkoutScenario("high_load", "solid push day, but the load ratio is high and weekly sets are low",
                    "bench 3x8@60 incline db press 3x10@50lb pushdown 3x12@30", ("good", "great"),
                    edits=[load(1.7)], checks=("no_added_volume",)),
    WorkoutScenario("lb_units", "a dumbbell session logged entirely in pounds",
                    "incline db press 3x10@60lb db shoulder press 3x10@40lb lateral raise 3x12@20lb",
                    ("great", "good", "mixed"), checks=("units_kept",)),
    WorkoutScenario("fitbit_only", "no sets logged: only a Fitbit run, with the knee flagged", None,
                    ("good", "mixed", "tough"), edits=[impact_spike], checks=("no_run_advice", "suggests_logging")),
    # ---- holdout: written before any output was read
    WorkoutScenario("ho_pr_red", "PRs yesterday, but recovery is red this morning",
                    "bench 3x8@62.5 incline db press 3x10@55lb pushdown 3x12@34", ("great",),
                    edits=[recovery(24, -15, -12, -6)], checks=("praises_progress", "no_hard_today"), split="holdout"),
    WorkoutScenario("ho_bodyweight", "a bodyweight pull session: more pull-up reps than last time",
                    "pull up 3x8 dip 3x10 push up 2x20", ("great", "good"),
                    checks=("praises_progress", "no_invented_weight"), split="holdout"),
    WorkoutScenario("ho_volume_cut", "the same weights as last time but one set of each",
                    "bench 1x8@60 incline db press 1x10@50lb lateral raise 1x12@8 pushdown 1x12@30",
                    ("mixed", "tough", "good"), checks=("no_hype",), split="holdout"),
    WorkoutScenario("ho_caution_pr", "a PR day while the load ratio is in the caution zone",
                    "bench 1x6@65 2x8@60 incline db press 3x10@55lb pushdown 3x12@32", ("great",),
                    edits=[load(1.42)], checks=("praises_progress", "no_added_volume"), split="holdout"),
]


def lift_sets(sc: WorkoutScenario) -> dict[str, list[dict]]:
    days = dict(sc.history)
    if sc.session:
        days[WORKOUT_DAY] = sc.session
    return {d: L.parse(t.split(), d + "T18:00") for d, t in days.items()}


def build(sc: WorkoutScenario) -> tuple[dict, dict, list[dict]]:
    """(model, context Claude sees, the workout's logged sets) for a scenario."""
    m = SC.base_model()
    for e in sc.edits:
        e(m)
    sets = lift_sets(sc)
    day = date.fromisoformat(m["date"])
    now = SC._now(sc.at)
    m["lifting"] = D._lifting(sets, day)
    # freshness from the logged sets, as build_model does with the user's own (the week's sessions cover its 7 days)
    m["muscles"] = D._muscles(m["workouts"]["week"], now, day, m["recovery"]["score"], sets)
    ctx = CO.workout_context(m, sets, now, sc.profile)
    wo = CO.latest_workout(m, sets, m["date"])
    return m, ctx, (wo or {}).get("entries") or []


def by_id(ids: str | None, split: str | None = None) -> list[WorkoutScenario]:
    pool = [s for s in WORKOUT_SCENARIOS if split in (None, "all", s.split)]
    if not ids:
        return pool
    want = set(ids.split(","))
    out = [s for s in WORKOUT_SCENARIOS if s.id in want]        # named ids win over --split (it defaults to dev)
    if want - {s.id for s in out}:
        raise SystemExit("unknown workout scenario(s): " + ", ".join(sorted(want - {s.id for s in out})))
    return out
