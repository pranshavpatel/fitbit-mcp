"""Lift log, recovery insights and the morning brief. Synthetic data only."""
import io
import json
import plistlib
import random
from datetime import datetime

import pytest

import brief as B
import dashboard
import data as D
import fixture_db
import lifts as L
import scores as S
from test_render import NOW, WIDTHS, _check, _model


# ---------------------------------------------------------------- parsing

def test_parse_exercises_sets_and_units():
    got = L.parse("bench 3x8@60 row 4x10@50 db bench 3x10@22.5 pullups 3x8 ohp 1x5@40 2x8@35 curls 3x12@25lb"
                  .split(), "t")
    assert [(e["exercise"], e["sets"], e["reps"], e["kg"]) for e in got] == [
        ("bench press", 3, 8, 60.0), ("row", 4, 10, 50.0), ("dumbbell press", 3, 10, 22.5),
        ("pull up", 3, 8, None), ("overhead press", 1, 5, 40.0), ("overhead press", 2, 8, 35.0),
        ("curl", 3, 12, 11.3)]


@pytest.mark.parametrize("text, msg", [("3x8", "no exercise"), ("bench", "no sets"), ("zumba 3x8", "don't know"),
                                       ("bench 30x8", "looks wrong"), ("", "nothing")])
def test_parse_errors(text, msg):
    with pytest.raises(L.LiftError, match=msg):
        L.parse(text.split(), "t")


def test_aliases():
    assert S.canonical_exercise("RDL") == "romanian deadlift"
    assert S.canonical_exercise("Barbell Row") == "row"
    assert S.canonical_exercise("db bench") == "dumbbell press"      # not the barbell bench
    assert S.canonical_exercise("lat-pulldown") == "lat pulldown"
    assert S.canonical_exercise("squats") == "squat"
    assert S.canonical_exercise("juggling") is None


def test_cli_log_show_undo(tmp_path, capsys):
    now = datetime(2026, 10, 8, 19, 30, tzinfo=D.NY)
    assert L.cli("bench 3x8@60 row 4x10@50".split(), tmp_path, now) == 0
    assert L.cli("yesterday squat 5x5@100".split(), tmp_path, now) == 0
    data = json.loads((tmp_path / "lift_log.json").read_text())["days"]
    assert [e["exercise"] for e in data["2026-10-08"]] == ["bench press", "row"]
    assert data["2026-10-08"][0]["at"] == "2026-10-08T19:30"
    assert data["2026-10-07"][0]["at"] == "2026-10-07T18:00"
    assert "chest 3" in capsys.readouterr().out
    assert L.cli(["undo"], tmp_path, now) == 0
    assert [e["exercise"] for e in L.load(tmp_path)["2026-10-08"]] == ["bench press"]
    L.cli([], tmp_path, now)
    assert "bench press 3x8@60" in capsys.readouterr().out
    assert L.cli("zumba 3x8".split(), tmp_path, now) == 2                     # nothing written on error
    assert len(L.load(tmp_path)["2026-10-08"]) == 1


def test_load_ignores_junk(tmp_path):
    (tmp_path / "lift_log.json").write_text('{"days": {"2026-10-01": [{"exercise": "zumba", "sets": 3, "reps": 8},'
                                            ' {"exercise": "bench", "sets": 3, "reps": 8, "kg": 60}]}}')
    assert [e["exercise"] for e in L.load(tmp_path)["2026-10-01"]] == ["bench press"]
    (tmp_path / "lift_log.json").write_text("not json")
    assert L.load(tmp_path) == {}


# ---------------------------------------------------------------- formulas

def test_weekly_sets_primary_full_secondary_half():
    sets = S.weekly_sets([{"exercise": "bench press", "sets": 3}, {"exercise": "row", "sets": 4}])
    assert sets["chest"] == 3 and sets["triceps"] == 1.5 and sets["lats"] == 4
    assert sets["shoulders"] == 1.5 + 2 and sets["biceps"] == 2 and sets["quads"] == 0


def test_e1rm_and_lift_dose():
    assert S.e1rm(60, 8) == pytest.approx(76.0)
    assert S.e1rm(100, 1) == 100 and S.e1rm(None, 8) is None
    dose = S.lift_dose([{"exercise": "bench press", "sets": 8}])
    assert dose["chest"] == pytest.approx(S.DOSE_REF)                        # 8 sets ≈ one typical session
    assert dose["triceps"] < dose["chest"] and "quads" not in dose
    assert S.lift_dose([{"exercise": "bench press", "sets": 40}])["chest"] == 100


def test_compare_needs_samples_and_grades_strength():
    assert S.compare([50] * 4, [40] * 10, "x") is None                       # too few on one side
    rnd = random.Random(1)
    a = [70 + rnd.gauss(0, 8) for _ in range(30)]
    b = [45 + rnd.gauss(0, 8) for _ in range(30)]
    eff = S.compare(a, b, "x")
    assert eff.strength == "clear" and eff.diff > 15
    noise = S.compare([50 + rnd.gauss(0, 15) for _ in range(8)], [50 + rnd.gauss(0, 15) for _ in range(8)], "y")
    assert noise.strength in ("unclear", "likely")


# ---------------------------------------------------------------- model + render

LOG = {"2026-09-21": "bench 3x8@57.5 squat 4x6@90", "2026-09-28": "bench 3x8@60 squat 4x6@95",
       "2026-10-05": "bench 4x8@62.5 incline 3x10@40 dips 3x12 pushdown 3x12@25",
       "2026-10-07": "row 4x10@50 pullups 4x8 curls 3x12@12"}


@pytest.fixture
def logged_model(tmp_path, monkeypatch):
    days = {d: L.parse(t.split(), d + "T19:00") for d, t in LOG.items()}
    L.save(tmp_path, days)
    return _model(tmp_path, monkeypatch)


def test_lifting_model(logged_model):
    lf = logged_model["lifting"]
    assert lf["has_log"] and lf["logged_days_week"] == ["2026-10-05", "2026-10-07"]
    assert lf["sets"]["chest"] == 4 + 3 + 3 and lf["sets"]["lats"] == 8
    assert "quads" in lf["under"]
    bench = next(r for r in lf["lifts"] if r["exercise"] == "bench press")
    assert [p["date"] for p in bench["points"]] == ["2026-09-21", "2026-09-28", "2026-10-05"]
    assert bench["pr"] and bench["change_pct"] > 0
    assert not any(r["exercise"] == "pull up" for r in lf["lifts"])          # bodyweight: no 1RM


def test_logged_sets_drive_muscle_freshness(logged_model):
    mu = logged_model["muscles"]
    src = {s["date"]: s["source"] for s in mu["sessions"]}
    assert src["2026-10-07"] == "sets" and src["2026-10-05"] == "sets"
    fresh = {r["muscle"]: r["fresh"] for r in mu["rows"]}
    assert fresh["lats"] < 100 and fresh["biceps"] < 100                      # trained yesterday, from the log
    assert sum(1 for s in mu["sessions"] if s["date"] == "2026-10-07") == 1   # not double-counted with Fitbit's lift


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("section", ("lifting", "insights", "freshness"))
def test_new_sections_fit_with_a_log(logged_model, width, section):
    buf = io.StringIO()
    dashboard.render(logged_model, dashboard.make_console(width, no_color=True, file=buf), width, section)
    lines = buf.getvalue().splitlines()
    _check(lines, width)
    text = "\n".join(lines)
    if section == "lifting":
        assert "bench press" in text and "★ PR" in text and "Hard sets per muscle" in text


def test_lifting_box_without_log_explains_how(tmp_path, monkeypatch):
    m = _model(tmp_path, monkeypatch)
    buf = io.StringIO()
    dashboard.render(m, dashboard.make_console(80, no_color=True, file=buf), 80, "lifting")
    assert "fitdash lift bench 3x8@60" in buf.getvalue()


def test_insights_model_and_caveat(logged_model):
    ins = logged_model["insights"]
    assert ins["n_days"] >= ins["min_days"]
    keys = {e["key"] for e in ins["effects"]}
    assert {"duration", "bedtime"} <= keys
    for e in ins["effects"]:
        assert e["n_yes"] >= 5 and e["n_no"] >= 5
        assert e["strength"] in ("clear", "likely", "unclear")
    buf = io.StringIO()
    dashboard.render(logged_model, dashboard.make_console(120, no_color=True, file=buf), 120, "insights")
    assert "not proof" in buf.getvalue()


def test_insights_wait_for_enough_history():
    m = D._insights(fixture_db.FIXTURE_DAY, {}, {}, {}, {}, {}, [], set())
    assert m["effects"] == [] and m["n_days"] == 0


def test_two_columns_place_new_boxes(logged_model):
    buf = io.StringIO()
    dashboard.render(logged_model, dashboard.make_console(160, no_color=True, file=buf), 160)
    text = buf.getvalue()
    assert "Lifting · sets & progress" in text and "What drives your recovery" in text


# ---------------------------------------------------------------- brief

def test_brief_compose(logged_model):
    title, body = B.compose(logged_model)
    assert title.startswith("Recovery ")
    assert "Next:" in body or "strain" in body.lower()
    assert len(title) < 80


def test_brief_compose_empty():
    assert B.compose({"has_data": False, "empty_reason": "x"})[1].startswith("No data")


def test_brief_cli_prints_and_notifies(tmp_path, monkeypatch, capsys):
    fixture_db.create(tmp_path / "fitbit.sqlite3")
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    sent = []
    monkeypatch.setattr(B, "notify", lambda t, b: sent.append((t, b)) or True)
    assert dashboard.main(["brief", "--no-sync"]) == 0
    assert sent and "Recovery" in capsys.readouterr().out
    sent.clear()
    assert dashboard.main(["brief", "--no-sync", "--print"]) == 0
    assert not sent


def test_osascript_quoting():
    assert B._osa_quote('say "hi" \\') == '"say \\"hi\\" \\\\"'


def test_brief_install_writes_launch_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(B.Path, "home", classmethod(lambda cls: tmp_path))
    calls = []

    class R:
        returncode, stderr = 0, ""

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, **k: calls.append(args) or R())
    assert B.install("9:05", tmp_path / ".fitbit-mcp") == 0
    pl = plistlib.loads(B.plist_path().read_bytes())
    assert pl["StartCalendarInterval"] == {"Hour": 9, "Minute": 5}
    assert pl["ProgramArguments"][-1] == "brief"
    assert pl["StandardOutPath"].endswith("brief.log")
    assert any(a[:2] == ["launchctl", "bootstrap"] for a in calls)
    assert B.install("25:00", tmp_path) == 2
    assert B.uninstall() == 0 and not B.plist_path().exists()


def test_brief_default_time():
    assert B.default_time({"wake_anchor": "08:30"}) == "9:00"
    assert B.default_time({"wake_anchor": "08:30", "brief_time": "07:45"}) == "07:45"
    assert B.default_time({}) == "09:00"


@pytest.mark.parametrize("typed, canon", [
    ("incline db press", "incline dumbbell press"), ("incline dbp", "incline dumbbell press"),
    ("decline flies", "chest fly"), ("machine chest press", "chest press"),
    ("cable lateral raises", "lateral raise"), ("single hand tricep pushdown", "single arm pushdown"),
    ("overhead tricep extensions", "triceps extension"), ("seated cable rows", "row"),
    ("latpulldown", "lat pulldown"), ("widegrip rows", "wide grip row"), ("lower back extensions", "back extension"),
    ("incline bicep curls", "incline curl"), ("preacher curls", "preacher curl"), ("archer pull", "archer pull")])
def test_gym_floor_names(typed, canon):
    assert S.canonical_exercise(typed) == canon
