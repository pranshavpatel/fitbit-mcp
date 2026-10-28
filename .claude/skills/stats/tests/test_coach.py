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


def test_parse_insights_accepts_fenced_json_and_rejects_junk():
    raw = 'Here you go:\n```json\n{"today": {"headline": "Train arms", "insights": [{"level": "good", "finding": "Recovery 82.", ' \
          '"action": "Lift by 18:00."}]}, "week": {"headline": "Sleep more", "insights": [{"level": "odd", "finding": "5.8 h avg."}]}}\n```'
    got = CO.parse_insights(raw)
    assert got["today"]["insights"][0] == {"level": "good", "finding": "Recovery 82.", "action": "Lift by 18:00."}
    assert got["week"]["insights"][0]["level"] == "watch" and got["week"]["insights"][0]["action"] == ""
    assert CO.parse_insights("no json here") is None
    assert CO.parse_insights('{"today": {"insights": []}, "week": {}}') is None


def test_insights_context_covers_today_and_the_week(m, tmp_path):
    ctx = CO.insights_context(m, at(12), "Goal: lean bulk", [], {"2026-10-08": [{"name": "wrap", "kcal": 500, "protein": 20}]})
    for key in ("recovery", "last_night", "strain_today", "activity_today", "tonight", "last_7_days", "workouts_7d",
                "this_week", "needs_attention", "weights_recent", "priorities", "profile"):
        assert key in ctx, key
    assert {"avg", "prev_week_avg", "daily"} <= set(ctx["this_week"]["metrics"][0])
    assert len(json.dumps(ctx, default=str)) < 30000


@pytest.mark.parametrize("width", WIDTHS)
def test_insights_command_renders_claude_and_fallback(tmp_path, monkeypatch, capsys, width):
    import fixture_db
    fixture_db.create(tmp_path / "fitbit.sqlite3")
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(CO, "ask", lambda *a, **k: None)
    assert dashboard.main(["insights", "--no-sync", "--no-color", "--width", str(width)]) == 0
    out = capsys.readouterr().out
    assert "Today ·" in out and "Past 7 days" in out and "auto: Claude unavailable" in out
    assert CO.load_insights(tmp_path) is None                                  # fallbacks aren't saved
    reply = json.dumps({"today": {"headline": "Arms today, asleep by 22:30.", "insights": [
        {"level": "flag", "finding": "Sleep debt 7h48; need 10h00 tonight.", "action": "In bed by 22:15."}]},
        "week": {"headline": "Sleep ran short.", "insights": [{"level": "watch", "finding": "5.8 h a night vs 7.1 h.",
                                                               "action": "Lights out 23:30 all week."}]}})
    monkeypatch.setattr(CO, "ask", lambda *a, **k: reply)
    assert dashboard.main(["insights", "--no-sync", "--no-color", "--width", str(width)]) == 0
    lines = capsys.readouterr().out.splitlines()
    _check(lines, min(width, 100))
    text = "\n".join(lines)
    assert "✕ Sleep debt 7h48" in text and "→ In bed by 22:15." in text and "written by Claude" in text
    assert CO.load_insights(tmp_path)["source"] == "claude"
    assert dashboard.main(["coach", "insights", "--last", "--no-color", "--width", str(width)]) == 0
    assert "In bed by 22:15." in capsys.readouterr().out


@pytest.mark.parametrize("text, state, expect", [
    ("Recovery 50% (Resting HR -20, HRV -6)", {}, "easy"),
    ("Sleep score 60 (poor), 5h10 asleep; weakest: hours vs need 70", {}, "asleep-by"),
    ("Load ratio 0.55 (low)", {"zone": "low"}, "Add one session"),
    ("Load ratio 1.42 (caution)", {"zone": "caution"}, "hold off"),
    ("Load ratio 1.70 (high)", {"zone": "high"}, "hold off"),
    ("Running/soccer spike this or last week (knee)", {}, "low-impact"),
    ("Sleep debt of 2h10 built up over the last week", {}, "asleep-by"),
    ("Bed and wake times vary a lot (consistency 40%)", {}, "30 minutes"),
    ("Gym 1/4 with 3 days left this week", {}, "calendar"),
    ("Weight below pace for your lean bulk (+0.02 kg/wk)", {}, "snack"),
    ("Weight above pace for your lean bulk (+0.60 kg/wk)", {}, "Trim"),
    ("Skin temp +0.9 °C off baseline", {}, "clinician"),
    ("SYNC", {}, "fitdash --sync"),
])
def test_fallback_actions_fit_each_attention_item(text, state, expect):
    note = "Last night isn't synced yet: open the Fitbit app on your phone, then run fitdash --sync."
    m = {"training": {"acwr": state}, "sleep": {"sync_note": {"text": note}}}
    assert expect in CO.attention_action(note if text == "SYNC" else text, m)


def test_insights_use_the_eval_chosen_model_unless_configured(monkeypatch):
    import subprocess
    seen = []
    monkeypatch.setattr(CO, "claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", ""))
    CO.generate_insights({})
    assert seen[-1][seen[-1].index("--model") + 1] == CO.INSIGHTS_MODEL == "haiku"
    CO.generate_insights({}, model="default")
    assert "--model" not in seen[-1]                                   # the CLI's own default
    CO.ask("s", "u")
    assert "--model" not in seen[-1]                                   # notes and reviews: not evaluated yet
