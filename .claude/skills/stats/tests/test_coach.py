"""Coach notes. Synthetic data only; the Claude CLI is always mocked."""
import io
import json
from datetime import datetime

import pytest

import coach as CO
import dashboard
import data as D
from test_render import WIDTHS, _check, _model


def at(h, mi=0, day=8):
    return datetime(2026, 10, day, h, mi, tzinfo=D.NY)


def model(workouts=(), has_night=True, asleep_by=22 * 60 + 40):
    return {"has_data": True, "date": "2026-10-08", "sleep": {"has_night": has_night, "tonight": {"asleep_by": asleep_by}},
            "workouts": {"week": list(workouts)}}


RUN = {"date": "2026-10-08", "start": "2026-10-08T17:00:00", "end": "2026-10-08T17:40:00", "label": "Run", "minutes": 40}


def test_coach_day_rolls_over_at_4am():
    assert CO.coach_day(at(0, 30, day=9)) == ("2026-10-08", 24 * 60 + 30)
    assert CO.coach_day(at(9)) == ("2026-10-08", 9 * 60)


def test_morning_due_once_after_sleep():
    assert CO.due(model(), [], at(8)) == ("morning", None)
    assert CO.due(model(has_night=False), [], at(8)) is None                 # wait for last night's sleep
    assert CO.due(model(has_night=False), [], at(10, 45)) == ("morning", None)
    done = [{"date": "2026-10-08", "at": "2026-10-08T08:00", "kind": "morning", "text": "x"}]
    assert CO.due(model(), done, at(9)) is None
    assert CO.due(model(), [], at(4, 30)) is None                             # too early


def test_activity_due_once_within_4h_and_not_if_covered():
    m = model([RUN])
    assert CO.due(m, [], at(18)) == ("activity", RUN)
    assert CO.due(m, [], at(22, 30)) == ("evening", None)                    # over 4 h later: no activity note
    noted = [{"date": "2026-10-08", "at": "2026-10-08T18:00", "kind": "activity", "text": "x",
              "trigger": "workout:" + RUN["start"]}]
    assert CO.due(m, noted, at(18, 30)) is None
    later = [{"date": "2026-10-08", "at": "2026-10-08T17:50", "kind": "note", "text": "x"}]
    assert CO.due(m, later, at(18)) is None                                   # a later note already covered it
    short = dict(RUN, minutes=6)
    assert CO.due(model([short]), [], at(18)) is None                         # a 6-minute walk isn't worth a note


def test_evening_due_before_asleep_by_and_after_midnight():
    assert CO.due(model(asleep_by=22 * 60), [], at(20, 30)) == ("evening", None)   # 90 min before 22:00
    assert CO.due(model(asleep_by=22 * 60), [], at(19, 59)) is None                # never before 20:00
    assert CO.due(model(asleep_by=None), [], at(21)) == ("evening", None)
    assert CO.due(model(), [], at(0, 30, day=9)) == ("evening", None)              # still yesterday's evening
    done = [{"date": "2026-10-08", "at": "2026-10-08T22:00", "kind": "evening", "text": "x"}]
    assert CO.due(model(), done, at(1, 0, day=9)) is None


def test_pick_and_store(tmp_path):
    CO.add(tmp_path, "morning", "**Recovery 70%.** Go.", at(8), "claude")
    CO.add(tmp_path, "evening", "Close it out.", at(22), "claude")
    notes = CO.load(tmp_path)
    assert notes[0]["text"] == "Recovery 70%. Go." and notes[0]["date"] == "2026-10-08"
    assert CO.pick(notes, "2026-10-08", at(23))["kind"] == "evening"
    assert CO.pick(notes, "2026-10-09", at(2, day=9))["kind"] == "evening"    # last night's note until 05:00
    assert CO.pick(notes, "2026-10-09", at(7, day=9)) is None
    CO.add(tmp_path, "evening", "late", at(0, 30, day=9), "claude")
    assert CO.load(tmp_path)[-1]["date"] == "2026-10-08"


def test_clean_caps_and_strips():
    assert CO.clean('"## Hi *there*"') == "Hi there"
    assert CO.clean(" ".join(["w"] * 200)).endswith("…")


def test_generate_uses_no_tools_and_handles_failure(monkeypatch):
    seen = {}

    class R:
        returncode, stdout = 0, "Recovery is green at 72%, so push today's pull session hard."

    def run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw["input"]
        return R()

    import subprocess
    monkeypatch.setattr(CO, "claude_bin", lambda: "/x/claude")
    monkeypatch.setattr(subprocess, "run", run)
    assert CO.generate({"note_type": "morning"}).startswith("Recovery is green")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd and "-p" in cmd
    assert '"note_type": "morning"' in seen["input"]
    R.returncode = 1
    assert CO.generate({"note_type": "morning"}) is None
    monkeypatch.setattr(CO, "claude_bin", lambda: None)
    assert CO.generate({"note_type": "morning"}) is None


@pytest.fixture
def m(tmp_path, monkeypatch):
    return _model(tmp_path, monkeypatch)


def test_context_is_compact_and_complete(m):
    ctx = CO.context(m, "morning", None, at(8), "Goal: lean bulk")
    for key in ("recovery", "last_night", "strain_today", "training", "tonight", "profile"):
        assert key in ctx
    assert len(json.dumps(ctx, default=str)) < 20000                          # summaries, not raw samples
    assert len(ctx["last_7_days"]) == 7 and "body" in ctx and "workouts_7d" in ctx


@pytest.mark.parametrize("width", WIDTHS)
def test_today_box_shows_a_note(tmp_path, monkeypatch, width):
    CO.add(tmp_path, "morning", "Recovery is green; train pull hard and get 10k steps.", at(9), "claude")
    mm = _model(tmp_path, monkeypatch)
    buf = io.StringIO()
    dashboard.render(mm, dashboard.make_console(width, no_color=True, file=buf), width, "today")
    lines = buf.getvalue().splitlines()
    _check(lines, width)
    text = buf.getvalue()
    assert "COACH" in text and "Morning · 9:00 am" in text and "train pull hard" in text


def test_today_box_falls_back_to_auto(m):
    buf = io.StringIO()
    dashboard.render(m, dashboard.make_console(100, no_color=True, file=buf), 100, "today")
    assert "COACH" in buf.getvalue() and "auto" in buf.getvalue()


def test_cli_set_show_and_run_without_claude(tmp_path, monkeypatch, capsys):
    import fixture_db
    fixture_db.create(tmp_path / "fitbit.sqlite3")
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    assert dashboard.main(["coach", "set", "note", "Deload", "week."]) == 0
    assert CO.load(tmp_path)[-1]["text"] == "Deload week." and CO.load(tmp_path)[-1]["source"] == "manual"
    monkeypatch.setattr(CO, "generate", lambda ctx: None)
    assert dashboard.main(["coach", "run", "--kind", "morning", "--force", "--no-sync"]) == 1   # reported, nothing saved
    assert len(CO.load(tmp_path)) == 1
    monkeypatch.setattr(CO, "generate", lambda ctx: "Sleep was short; keep strain under 10 today.")
    assert dashboard.main(["coach", "run", "--kind", "morning", "--force", "--no-sync"]) == 0
    assert CO.load(tmp_path)[-1]["source"] == "claude"
