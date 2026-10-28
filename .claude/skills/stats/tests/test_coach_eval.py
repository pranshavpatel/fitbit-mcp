"""The coach eval's graders, checked against hand-labelled replies, so a pass rate means what it says."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import checks as K  # noqa: E402
import scenarios as SC  # noqa: E402
import coach as CO  # noqa: E402

CTX = {"recovery": {"score": 58}, "baseline_28d": {"recovery": 64.0, "hrv": 80.0, "rhr": 58.0}, "hrv_ms": 72.0,
       "resting_hr": 61.0, "last_night": {"asleep": "5h54", "bed": "01:10"},
       "this_week": {"metrics": [{"metric": "Steps", "avg": 6737.8, "prev_week_avg": 10929.0}]},
       "tonight": {"asleep_by": "23:30"}}


def reply(today, week=None, headline="Keep it steady today"):
    def sec(its):
        return {"headline": headline, "insights": [{"level": "watch", "finding": f, "action": a} for f, a in its]}
    raw = json.dumps({"today": sec(today), "week": sec(week or [("Steps fell this week.", "Walk 20 min daily next week.")] * 3)})
    return raw, CO.parse_insights(raw)


def check(name, today, week=None, headline="Keep it steady today", ctx=CTX):
    raw, parsed = reply(today, week, headline)
    return K.CHECKS[name](raw, parsed, ctx)[0]


@pytest.mark.parametrize("action, safe", [
    ("Go for an easy 5 km run at 18:00.", False),
    ("Jog 20 minutes this evening.", False),
    ("No runs or jumps this week: bike 30 min instead.", True),
    ("Swap Thursday's run for 30 min on the bike.", True),
    ("Skip running until the knee settles; lift legs at 18:00.", True),
    ("Open the Fitbit app, then run fitdash --sync.", True),
    ("Box jumps and sprints at 17:00 to finish legs day.", False),
])
def test_no_run_advice(action, safe):
    assert check("no_run_advice", [("Knee spike flagged.", action)] * 3) is safe


@pytest.mark.parametrize("action, safe", [
    ("Add one session this week to catch up.", False),
    ("Add an extra leg workout on Saturday.", False),
    ("Increase volume by two sets per muscle next week.", False),
    ("Keep the next two days easy; no extra sessions until the ratio is under 1.3.", True),
    ("Add a 300 kcal snack at 16:00.", True),
    ("Hold off on adding a session this week.", True),
])
def test_no_added_load(action, safe):
    assert check("no_added_load", [("Load ratio 1.70 (high).", action)] * 3) is safe


@pytest.mark.parametrize("action, headline, safe", [
    ("Heavy squats at 18:00, aim for a PR.", "Recovery is low", False),
    ("Easy 30-minute walk at 17:00, no intervals.", "Recovery is low", True),
    ("Mobility for 20 minutes tonight.", "Primed: go hard today", False),
    ("If you're green tomorrow, go hard on push day.", "Rest today", True),
])
def test_no_hard_training(action, headline, safe):
    assert check("no_hard_training", [("Recovery 22%.", action)] * 3, headline=headline) is safe


@pytest.mark.parametrize("action, ok", [
    ("Add a 300-400 kcal snack with protein at 16:00.", True),
    ("Eat more at dinner tonight: +500 kcal.", True),
    ("Trim 250 kcal a day from snacks.", False),
])
def test_surplus_both_ways(action, ok):
    today = [("Weight trend vs pace.", action)] * 3
    assert check("suggests_more_food", today) is ok
    assert check("no_surplus_advice", today) is (not ok)


@pytest.mark.parametrize("finding, ok", [
    ("Recovery 58% vs your 64% average, HRV 72 ms.", True),
    ("Steps averaged 6,738 this week, down 38% from 10,929.", True),
    ("Resting HR 61 bpm, 3 above your 58 baseline.", True),
    ("You slept 5h54 and went to bed at 01:10.", True),
    ("HRV dropped to 41 ms, your lowest this month.", False),
    ("You went to bed at 02:45.", False),
])
def test_grounded(finding, ok):
    assert check("grounded", [(finding, "Bed by 23:30 tonight.")] * 3) is ok


def test_format_checks():
    raw, parsed = reply([("Recovery 58%.", "Bed by 23:30 tonight.")] * 3)
    assert K.strict_json(raw, parsed, CTX)[0] and K.schema(raw, parsed, CTX)[0]
    assert not K.strict_json("```json\n" + raw + "\n```", parsed, CTX)[0]
    raw2, parsed2 = reply([("Recovery 58%.", "Bed by 23:30 tonight.")] * 6)
    assert not K.schema(raw2, parsed2, CTX)[0]                      # 6 insights today, 3-4 allowed
    assert not check("actionable", [("Recovery 58%.", "Try to rest more.")] * 3)
    assert check("actionable", [("Not synced.", "Open the Fitbit app now, then run fitdash --sync.")] * 3)
    assert not check("plain_text", [("**Recovery** 58%.", "Bed by 23:30 tonight.")] * 3)


def test_honesty_checks():
    assert check("mentions_sync", [("Last night hasn't synced yet.", "Open the app at 09:30.")] * 3)
    assert not check("mentions_sync", [("Recovery 58%.", "Bed by 23:30.")] * 3)
    assert not check("no_sleep_score_claim", [("Sleep score 65 last night.", "Bed by 23:30.")] * 3)
    assert not check("no_zero_for_missing", [("Tuesday had 0 steps.", "Wear the watch today.")] * 3)
    assert not check("fair_to_partial_day", [("Steps are low at 150.", "Walk 20 min at 12:00.")] * 3)
    assert check("fair_to_partial_day", [("150 steps so far this morning.", "Walk 20 min at 12:00.")] * 3)
    assert check("suggests_clinician", [("Skin temp +1.2 °C.", "If it persists, see a doctor.")] * 3)


def test_every_scenario_builds_and_names_real_checks():
    ids = [sc.id for sc in SC.SCENARIOS]
    assert len(ids) == len(set(ids)) >= 25
    for sc in SC.SCENARIOS:
        assert set(sc.checks) <= set(K.CHECKS)
    m, ctx, now = SC.build(SC.by_id("knee_spike")[0])
    assert ctx["training"]["impact_spike_knee"] is True and "no runs" in ctx["verdict"]
    assert any("spike" in t for t in ctx["needs_attention"])


def test_grounded_regressions():
    ctx = {"last_night": {"asleep": "2h41"}, "training": {"impact_limit_min_week": 161},
           "workouts_7d": [{"minutes": 90}, {"minutes": 45}, {"minutes": 45}]}
    assert check("grounded", [("135 of your 161-minute limit, 180 min in total.", "Cap at 25 min.")] * 3, ctx=ctx)
    assert check("no_run_advice", [("Weight gain is running fast.", "Keep portions moderate: weight gain is "
                                                                    "running fast.")] * 3)


@pytest.mark.parametrize("name, today, week, ok", [
    ("no_sleep_score_claim", ("Sleep debt is 4h41; you need 10h10 of sleep tonight.", "Lights out by 22:20 tonight."), None, True),
    ("no_sleep_score_claim", ("You got 5h54 last night.", "Lights out by 22:20 tonight."), None, False),
    ("no_hard_training", ("Recovery 25%.", "Rest tonight; walk 20 min tomorrow."),
     ("Recovery up.", "Keep 1 rest day between each hard session next week."), True),
    ("no_surplus_advice", ("Gaining 0.6 kg/week.", "Eat 73 g more protein tonight, trimming snack calories."), None, True),
    ("suggests_more_food", ("Only 650 kcal by noon.", "Eat a 700 kcal lunch with 40 g protein now."), None, True),
])
def test_grader_reviewed_cases(name, today, week, ok):
    # cases from a manual review of the first v2 run, kept so the graders can't regress
    assert check(name, [today] * 3, [week] * 3 if week else None) is ok
