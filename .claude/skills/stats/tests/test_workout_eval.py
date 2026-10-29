"""The workout-review graders (evals/workout_checks.py) on hand-labelled replies: each check must pass
the good reply and catch the bad one, so a score means what it says."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import coach as CO  # noqa: E402
import workout_checks as W  # noqa: E402
import workout_scenarios as WS  # noqa: E402

BUILT = {}


def built(sid):
    if sid not in BUILT:
        sc = WS.by_id(sid)[0]
        BUILT[sid] = (sc, *WS.build(sc))
    return BUILT[sid]


def reply(sid, headline="Bench up 2.5 kg: a strong push day.", rating="great", summary="", exercises=None, tips=None):
    sc, m, ctx, entries = built(sid)
    if exercises is None:
        exercises = [{"exercise": r["exercise"], "level": "good", "note": "Top set " + r["top_set"] + ".",
                      "next": "Next time: 1 more rep per set."} for r in ctx["session"]["exercises"]]
    if tips is None:
        tips = [{"level": "good", "finding": "Chest is at 4 sets this week.", "action": "Sleep 8 h tonight."},
                {"level": "watch", "finding": "Triceps at 3 sets.", "action": "Add triceps on Friday."}]
    raw = json.dumps({"workout": {"headline": headline, "rating": rating, "summary": summary,
                                  "exercises": exercises, "tips": tips}})
    return raw, CO.parse_workout(raw), ctx, sc


def check(name, *args):
    return W.CHECKS["w_" + name](*args)[0]


def test_every_scenario_builds_and_names_real_checks():
    for sc in WS.WORKOUT_SCENARIOS:
        m, ctx, entries = WS.build(sc)
        assert ctx is not None and set(sc.expect) <= set(W.RATINGS)
        assert all("w_" + c in W.CHECKS for c in sc.checks), sc.id
    assert {s.split for s in WS.WORKOUT_SCENARIOS} == {"dev", "holdout"}


def test_rule_based_fallback_passes_its_own_eval():
    for sc in WS.WORKOUT_SCENARIOS:
        m, ctx, entries = WS.build(sc)
        raw = json.dumps({"workout": CO.fallback_workout(ctx, entries)})
        failed = [k for k, (ok, _) in W.run(raw, CO.parse_workout(raw), ctx, sc).items() if not ok]
        assert not failed, (sc.id, failed)


def test_format_checks():
    assert check("strict_json", *reply("pr_day"))
    raw, parsed, ctx, sc = reply("pr_day")
    assert not check("strict_json", "```json\n" + raw + "\n```", parsed, ctx, sc)
    assert check("schema", *reply("pr_day"))
    assert not check("schema", *reply("pr_day", rating="awesome"))
    assert not check("schema", *reply("pr_day", exercises=[]))                       # one per logged exercise
    assert not check("schema", *reply("pr_day", tips=[{"level": "good", "finding": "x", "action": "y"}]))
    assert not check("lengths", *reply("pr_day", headline=" ".join(["word"] * 17)))
    assert not check("plain_text", *reply("pr_day", headline="**Huge** PR day"))


@pytest.mark.parametrize("note, ok", [
    ("Top set 62.5 kg x 8.", True),
    ("62.5 kg x 8, up from 60 kg x 8 on Wednesday.", True),                     # both in the data
    ("50 lb is 22.7 kg per hand.", True),                                        # a unit conversion of a logged weight
    ("Estimated 1RM 79.2 kg.", True),
    ("Estimated 1RM 85 kg, a 12% jump.", False),                                 # invented
])
def test_grounded(note, ok):
    raw, parsed, ctx, sc = reply("pr_day")
    parsed["exercises"][0]["note"] = note
    assert check("grounded", raw, parsed, ctx, sc) == ok


@pytest.mark.parametrize("note, ok", [
    ("First time logged: 3x10 at 24 kg. The baseline to beat.", True),
    ("Up from last time: 3x10 at 24 kg.", False),
    ("A new PR at 24 kg x 10.", False),
])
def test_no_false_comparison(note, ok):
    raw, parsed, ctx, sc = reply("first_time")
    parsed["exercises"][0]["note"] = note
    assert check("no_false_comparison", raw, parsed, ctx, sc) == ok


@pytest.mark.parametrize("nxt, ok", [("Next time: 65 lb for 3x10.", True), ("Next time: 30 kg for 3x10.", False),
                                     ("Next time: 3x11 at 60.", True)])
def test_units_kept(nxt, ok):
    raw, parsed, ctx, sc = reply("lb_units")
    parsed["exercises"][0]["next"] = nxt
    assert check("units_kept", raw, parsed, ctx, sc) == ok


@pytest.mark.parametrize("nxt, ok", [("Next time: 3x9 pull-ups.", True), ("Next time: 3x8 at 10 kg.", False),
                                     ("Next time: add a 5 kg belt for 3x6.", True)])
def test_no_invented_weight(nxt, ok):
    raw, parsed, ctx, sc = reply("ho_bodyweight")
    parsed["exercises"][0]["next"] = nxt
    assert check("no_invented_weight", raw, parsed, ctx, sc) == ok


def test_rating_and_hype():
    assert check("rating_fits", *reply("tough_day", rating="tough"))
    assert not check("rating_fits", *reply("tough_day", rating="great"))
    assert not check("no_hype", *reply("tough_day", headline="Great session, crushed it!", rating="tough"))
    assert check("no_hype", *reply("tough_day", headline="Bench held, but the presses slipped after a short night.",
                                   rating="tough"))
    assert check("no_hype", *reply("mixed_day", headline="Great bench, but incline reps fell off.", rating="mixed"))


@pytest.mark.parametrize("action, ok", [
    ("Cycle 30 min tomorrow; no runs or jumps.", True),
    ("Swap the run for a 30 min bike ride tomorrow.", True),
    ("Go for an easy 5 km run tomorrow.", False),
    ("Add box jumps on Friday.", False),
])
def test_no_run_advice(action, ok):
    raw, parsed, ctx, sc = reply("knee_legs")
    parsed["tips"][0]["action"] = action
    assert check("no_run_advice", raw, parsed, ctx, sc) == ok


@pytest.mark.parametrize("action, ok", [
    ("Keep the same 9 sets next week.", True),
    ("Don't add sets until the load ratio drops.", True),
    ("Add 4 sets of chest on Friday.", False),
    ("Add an extra session this week.", False),
])
def test_no_added_volume(action, ok):
    raw, parsed, ctx, sc = reply("high_load")
    parsed["tips"][0]["action"] = action
    assert check("no_added_volume", raw, parsed, ctx, sc) == ok


@pytest.mark.parametrize("action, ok", [
    ("Rest today and sleep 9 h tonight.", True),
    ("Next session: bench 65 kg x 8, a heavy top set.", True),        # not today
    ("Go hard on legs today.", False),
])
def test_no_hard_today(action, ok):
    raw, parsed, ctx, sc = reply("ho_pr_red")
    parsed["tips"][0]["action"] = action
    assert check("no_hard_today", raw, parsed, ctx, sc) == ok


def test_usefulness_checks():
    assert check("praises_progress", *reply("pr_day", headline="Four PRs: bench, incline, laterals and pushdowns."))
    assert not check("praises_progress", *reply("pr_day", headline="Push day logged: 12 sets.", rating="good"))
    raw, parsed, ctx, sc = reply("pr_day")
    parsed["exercises"][0]["next"] = "Keep going."
    assert not check("next_targets", raw, parsed, ctx, sc)
    parsed["tips"][0]["action"] = "Recover well."
    assert not check("actionable", raw, parsed, ctx, sc)
    sc, m, ctx, entries = built("fitbit_only")
    raw = json.dumps({"workout": {"headline": "A 30 min run.", "rating": "good", "summary": "", "exercises": [],
                                  "tips": [{"level": "good", "finding": "Run at 150 bpm.", "action": "Rest tomorrow."}]}})
    assert not check("suggests_logging", raw, CO.parse_workout(raw), ctx, sc)


def test_reviewed_false_positives_stay_fixed():
    """Replies read during the grader review (docs/coach-eval.md): each was flagged wrongly."""
    raw, parsed, ctx, sc = reply("fitbit_only", exercises=[])
    parsed["summary"] = "You spent 37 of 45 minutes in zones 4 and 5."                  # 19 + 18
    assert check("grounded", raw, parsed, ctx, sc)
    raw, parsed, ctx, sc = reply("ho_bodyweight")
    parsed["summary"] = "94 reps across pull-ups, dips and push-ups."                    # 24 + 30 + 40
    assert check("grounded", raw, parsed, ctx, sc)
    raw, parsed, ctx, sc = reply("ho_pr_red")
    parsed["tips"][0]["action"] = "Wait until recovery is out of red, likely tomorrow, before training heavy today."
    assert check("no_hard_today", raw, parsed, ctx, sc)
    raw, parsed, ctx, sc = reply("knee_legs")
    parsed["tips"][0]["action"] = "Bike 30 min tomorrow. If the knee aches, see a clinician before running again."
    assert check("no_run_advice", raw, parsed, ctx, sc)
    assert check("praises_progress", *reply("ho_bodyweight", headline="Pull-up PR at 3x8, but fewer sets.",
                                            rating="mixed"))


def test_review_fixes_tighten_the_graders():
    """Holes found in code review (PR #3), closed before the re-run."""
    assert not check("lengths", *reply("pr_day", headline=" ".join(["word"] * 17)))       # prompt says 14
    raw, parsed, ctx, sc = reply("pr_day")
    swapped = [dict(e, exercise="squat") for e in parsed["exercises"]]                  # right count, wrong lifts
    assert not check("schema", *reply("pr_day", exercises=swapped))
    k_raw, k_parsed, k_ctx, k_sc = reply("knee_legs")                                    # no 5 anywhere in its data
    k_parsed["exercises"][0]["note"] = "Leg press 150 kg x 10, 5 kg over your best."     # an invented small weight
    assert not check("grounded", k_raw, k_parsed, k_ctx, k_sc)
    k_parsed["exercises"][0]["note"] = "Leg press 150 kg x 10 on all 3 sets, 5 of them at full depth."
    assert check("grounded", k_raw, k_parsed, k_ctx, k_sc)                              # a small count is fine
    parsed["exercises"][0]["next"] = "Next time: 9 sets."
    assert not check("next_targets", raw, parsed, ctx, sc)
    for nxt in ("Next time: 62.5 kg for 3x8.", "Aim for 2 more reps per set.", "Next time: 3x9."):
        parsed["exercises"][0]["next"] = nxt
        assert check("next_targets", raw, parsed, ctx, sc), nxt
    raw, parsed, ctx, sc = reply("high_load")
    parsed["tips"][0]["action"] = "Increase sets on chest by Friday."
    assert not check("no_added_volume", raw, parsed, ctx, sc)
    raw, parsed, ctx, sc = reply("ho_pr_red")
    parsed["tips"][0]["action"] = "Heavy squats at 18:00."
    assert not check("no_hard_today", raw, parsed, ctx, sc)
    parsed["tips"][0]["action"] = "Tomorrow at 18:00: heavy squats if recovery is green."
    assert check("no_hard_today", raw, parsed, ctx, sc)


def test_eval_freshness_comes_from_the_logged_sets():
    m, ctx, entries = WS.build(WS.by_id("pr_day")[0])
    assert ctx["muscle_freshness_now_pct"]["chest"] < 80          # pushed yesterday: not fully fresh


def test_parse_workout_repairs_a_reply_with_no_closing_brace():
    got = CO.parse_workout('{"headline": "Solid day", "rating": "good", "tips": [{"finding": "Chest 12 sets.", "action": "Hold."}')
    assert got and got["tips"][0]["finding"] == "Chest 12 sets."
