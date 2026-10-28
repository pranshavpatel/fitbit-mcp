"""fitdash insights --workout: the most recent workout, reviewed exercise by exercise."""
import json

import pytest

import coach as CO
import dashboard
import data as D
import lifts as L
from test_render import NOW, WIDTHS, _check, _model

PUSH_1 = "incline db press 1x12@35lb 1x12@50lb 1x7@50lb pushdown 3x12@36"
PUSH_2 = "incline db press 1x12@30lb 1x12@50lb 1x8@50lb pushdown 1x12@41 1x6@50 lateral raise 2x12@11"


def _log(tmp_path, days: dict[str, str]):
    L.save(tmp_path, {d: L.parse(t.split(), d + "T18:00") for d, t in days.items()})


def test_parse_workout_cleans_and_rejects_junk():
    raw = '```json\n{"workout": {"headline": "Two PRs", "rating": "amazing", "summary": "18 sets.", "exercises": [' \
          '{"exercise": "pushdown", "level": "odd", "note": "PR at 50 kg x 6.", "next": "50 kg x 8."}, {"note": ""}],' \
          ' "tips": [{"level": "good", "finding": "Chest at 17 sets.", "action": "Hold next week."}]}}\n```'
    got = CO.parse_workout(raw)
    assert got["rating"] == "good"                                  # unknown rating → neutral
    assert got["exercises"] == [{"exercise": "pushdown", "level": "watch", "note": "PR at 50 kg x 6.", "next": "50 kg x 8."}]
    assert got["tips"][0]["action"] == "Hold next week."
    assert CO.parse_workout("no json") is None
    assert CO.parse_workout('{"workout": {"headline": "", "tips": []}}') is None
    assert CO.parse_workout('{"workout": {"headline": "Hi"}}') is None   # nothing to show


def test_context_compares_each_exercise_with_last_time(tmp_path, monkeypatch):
    _log(tmp_path, {"2026-10-03": PUSH_1, "2026-10-07": PUSH_2})
    m = _model(tmp_path, monkeypatch)
    ctx = CO.workout_context(m, L.load(tmp_path), NOW, "Knee: no jumps")
    assert ctx["workout_date"] == "Wednesday 2026-10-07" and ctx["days_ago"] == 1
    ses = ctx["session"]
    assert ses["kind"] == "push" and ses["sets"] == 7
    by = {r["exercise"]: r for r in ses["exercises"]}
    assert list(by) == ["incline dumbbell press", "triceps pushdown", "lateral raise"]      # order done
    inc = by["incline dumbbell press"]
    assert inc["set_list"] == ["1x12@30lb", "1x12@50lb", "1x8@50lb"]                         # units as typed
    assert inc["last_time"]["set_list"] == ["1x12@35lb", "1x12@50lb", "1x7@50lb"] and not inc["pr"]
    assert by["triceps pushdown"]["pr"] and by["triceps pushdown"]["e1rm_change_pct"] == 19.0
    assert by["lateral raise"]["first_time_logged"] and by["lateral raise"]["last_time"] is None
    assert ses["prs"] == ["triceps pushdown"]
    assert ctx["previous_same_kind"]["date"] == "Saturday 2026-10-03"
    assert ctx["profile"] == "Knee: no jumps" and "recovery_today" in ctx
    assert len(json.dumps(ctx, default=str)) < 20000


def test_context_without_any_workout_is_none(tmp_path, monkeypatch):
    m = _model(tmp_path, monkeypatch)
    m["workouts"]["week"] = []
    assert CO.workout_context(m, {}, NOW) is None


def test_fallback_names_prs_and_next_targets(tmp_path, monkeypatch):
    _log(tmp_path, {"2026-10-03": PUSH_1, "2026-10-07": PUSH_2})
    m = _model(tmp_path, monkeypatch)
    sets = L.load(tmp_path)
    ctx = CO.workout_context(m, sets, NOW)
    got = CO.fallback_workout(ctx, sets["2026-10-07"])
    assert got["rating"] == "great" and "triceps pushdown" in got["headline"]
    nxt = {e["exercise"]: e["next"] for e in got["exercises"]}
    assert nxt["incline dumbbell press"] == "Next time: stay at 50lb and aim for 10 reps on every set."
    assert nxt["lateral raise"] == "Next time: try 13.5kg for 12 reps."
    assert all(e["note"] for e in got["exercises"]) and 1 <= len(got["tips"]) <= 4
    only_fitbit = dict(ctx, session=None, no_sets_logged=True,
                       fitbit_sessions=[{"what": "Run", "minutes": 30, "strain": 9.1, "avg_hr": 150}])
    got = CO.fallback_workout(only_fitbit, [])
    assert got["exercises"] == [] and "fitdash lift" in got["tips"][0]["action"]


@pytest.mark.parametrize("width", WIDTHS)
def test_workout_command_renders_claude_and_fallback(tmp_path, monkeypatch, capsys, width):
    _log(tmp_path, {"2026-10-03": PUSH_1, "2026-10-07": PUSH_2})
    _model(tmp_path, monkeypatch)                                 # fixture DB + config under FITBIT_MCP_HOME
    monkeypatch.setattr(CO, "ask", lambda *a, **k: None)
    args = ["insights", "--workout", "--no-sync", "--no-color", "--width", str(width)]
    assert dashboard.main(args) == 0
    out = capsys.readouterr().out
    assert "Workout · Wed Oct 7 · push" in out and "auto: Claude unavailable" in out
    assert CO.load_workout_insights(tmp_path) is None                          # fallbacks aren't saved
    reply = json.dumps({"workout": {"headline": "A pushdown PR: 50 kg x 6, up 19%.", "rating": "great",
                                    "summary": "9 sets on push day, one more exercise than Saturday.",
                                    "exercises": [{"exercise": "triceps pushdown", "level": "good",
                                                   "note": "PR: 50 kg x 6 after 41 kg x 12.", "next": "50 kg x 8 next time."}],
                                    "tips": [{"level": "watch", "finding": "Shoulders at 2 sets this week.",
                                              "action": "Add 3 sets of lateral raises on Friday."}]}})
    monkeypatch.setattr(CO, "ask", lambda *a, **k: reply)
    assert dashboard.main(args) == 0
    lines = capsys.readouterr().out.splitlines()
    _check(lines, min(width, 100))
    text = "\n".join(lines)
    assert "✓ A pushdown PR" in text and "→ 50 kg x 8 next time." in text and "written by Claude" in text
    assert CO.load_workout_insights(tmp_path)["date"] == "2026-10-07"
    assert dashboard.main(["insights", "--workout", "--last", "--no-color", "--width", str(width)]) == 0
    assert "Add 3 sets of lateral raises" in capsys.readouterr().out


def test_logged_sets_name_an_untagged_split_day_and_move_the_queue():
    split = ["push (chest/tri)", "pull (back/bi)", "legs/abs", "arms"]
    w = [{"date": d, "start": d + "T17:00", "type": "STRENGTH_TRAINING"} for d in ("2026-10-02", "2026-10-05", "2026-10-07")]
    cfg = {"split": split, "split_anchor": {"date": "2026-10-01", "day": "push (chest/tri)"},
           "lift_sets": {"2026-10-07": L.parse(PUSH_2.split(), "2026-10-07T18:00")}}
    D._label_lifts(w, cfg)
    # no session on the anchor date: the first one after it is the next day in the rotation
    assert [x["split"] for x in w] == ["pull (back/bi)", "legs/abs", "push (chest/tri)"]
    assert [x["split_source"] for x in w] == ["assumed", "assumed", "logged"]
    nxt = D._split_next(cfg, w)
    assert nxt["next"] == "pull (back/bi)" and nxt["last_done"] == "push (chest/tri)" and nxt["last_done_date"] == "2026-10-07"


def test_parse_workout_survives_non_list_fields():
    raw = '{"workout": {"headline": "Good day", "exercises": 3, "tips": [{"finding": "Chest 17 sets.", "action": "Hold."}, "x"]}}'
    got = CO.parse_workout(raw)
    assert got["exercises"] == [] and got["tips"][0]["finding"] == "Chest 17 sets."
    assert CO.parse_workout('{"workout": {"headline": "Hi", "exercises": "a", "tips": {}}}') is None


def test_fallback_holds_volume_at_high_load_and_rates_mixed_days(tmp_path, monkeypatch):
    _log(tmp_path, {"2026-10-03": PUSH_1, "2026-10-07": PUSH_2})
    m = _model(tmp_path, monkeypatch)
    sets = L.load(tmp_path)
    ctx = CO.workout_context(m, sets, NOW)
    ctx["training"]["load_zone"] = "high"
    ctx["week_sets_per_muscle"]["last_7_days"] = {"chest": 3, "triceps": 4, "shoulders": 2}     # all under 10
    got = CO.fallback_workout(ctx, sets["2026-10-07"])
    assert not any(t["action"].startswith("Add") for t in got["tips"])
    assert "no extra sessions or volume" in got["tips"][0]["action"]
    inc = next(r for r in ctx["session"]["exercises"] if r["exercise"] == "incline dumbbell press")
    inc["e1rm_change_pct"] = -8.0                                     # a PR elsewhere, but this lift slipped
    got = CO.fallback_workout(ctx, sets["2026-10-07"])
    assert got["rating"] == "mixed" and "1 lift slipped" in got["headline"]


def test_split_labels_ignore_zero_credit_sets_and_pre_anchor_tags():
    split = ["push (chest/tri)", "pull (back/bi)", "legs/abs", "arms"]
    w = [{"date": d, "start": d + "T17:00", "type": "STRENGTH_TRAINING"} for d in ("2026-09-28", "2026-10-02", "2026-10-04")]
    cfg = {"split": split, "split_anchor": {"date": "2026-10-01", "day": "push (chest/tri)"},
           "lift_log": {"2026-09-28": "legs/abs"},                                     # before the anchor
           "lift_sets": {"2026-10-04": L.parse("box jump 3x5".split(), "2026-10-04T18:00")}}
    D._label_lifts(w, cfg)
    assert [x["split"] for x in w] == ["legs/abs", "pull (back/bi)", "legs/abs"]
    assert [x["split_source"] for x in w] == ["logged", "assumed", "assumed"]       # box jumps name no day


def test_insights_command_passes_the_configured_model(tmp_path, monkeypatch, capsys):
    _model(tmp_path, monkeypatch)
    cfg = json.loads((tmp_path / "coaching" / "stats.json").read_text())
    (tmp_path / "coaching" / "stats.json").write_text(json.dumps(dict(cfg, insights_model="sonnet")))
    seen = []
    monkeypatch.setattr(CO, "ask", lambda system, user, timeout=150, model=None: seen.append(model))
    assert dashboard.main(["insights", "--no-sync", "--no-color"]) == 0
    assert seen == ["sonnet"]
    assert dashboard.main(["insights", "--workout", "--no-sync", "--no-color"]) in (0, 1)
    assert seen[1:] in ([], [None])                                   # the workout review: the CLI's default
