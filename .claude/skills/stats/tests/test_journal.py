"""Journal & habit tracking. Synthetic data only."""
import io
import json
import random
from datetime import date, datetime, timedelta

import pytest

import dashboard
import data as D
import fixture_db
import journal as J
from test_render import WIDTHS, _check, _model

EVENING = datetime(2026, 10, 8, 21, 0, tzinfo=D.NY)
MORNING = datetime(2026, 10, 8, 8, 0, tzinfo=D.NY)


def test_parse_marks_prefixes_and_note():
    marks, note = J.parse(["+alc", "-screens", "+stretch", "note", "hot", "room"], J.DEFAULT_HABITS)
    assert marks == {"alcohol": True, "screens": False, "stretch": True} and note == "hot room"
    with pytest.raises(J.JournalError):
        J.parse(["+zzz"], J.DEFAULT_HABITS)
    with pytest.raises(J.JournalError):
        J.parse(["alcohol"], J.DEFAULT_HABITS)                       # needs + or -
    with pytest.raises(J.JournalError, match="2 habits"):
        J.parse(["+late"], J.DEFAULT_HABITS)                          # late-caffeine / late-meal


def test_default_day_is_yesterday_in_the_morning():
    assert J.default_day(MORNING) == date(2026, 10, 7)
    assert J.default_day(EVENING) == date(2026, 10, 8)


def test_cli_logs_merges_and_shows(tmp_path, capsys):
    cfg = {}
    assert J.cli(["+alcohol", "-screens"], tmp_path, cfg, EVENING, False) == 0
    assert J.cli(["+stretch", "note", "felt", "good"], tmp_path, cfg, EVENING, False) == 0
    e = J.load(tmp_path)["2026-10-08"]
    assert e["habits"] == {"alcohol": True, "screens": False, "stretch": True} and e["note"] == "felt good"
    assert J.cli(["yesterday", "-alcohol"], tmp_path, cfg, EVENING, False) == 0
    assert J.load(tmp_path)["2026-10-07"]["habits"] == {"alcohol": False}
    capsys.readouterr()
    J.cli(["show"], tmp_path, cfg, EVENING, False)
    shown = capsys.readouterr().out
    assert "✗ alcohol" in shown and "✓ stretch" in shown          # ✓ = a good day for that habit
    assert shown.index("To do") < shown.index("To avoid")
    assert J.cli(["2026-10-09", "+alcohol"], tmp_path, cfg, EVENING, False) == 2      # future
    assert J.cli(["+nope"], tmp_path, cfg, EVENING, False) == 2


def test_interactive_ask(tmp_path):
    # asked in groups: to do (stretch, protein), to avoid (alcohol, late-caffeine, late-meal, screens,
    # knee-pain), tracking (creatine); "maybe" is re-asked
    answers = iter(["y", "n", "", "maybe", "y", "", "", "", "", "slept ok"])
    marks, note = J.ask(J.DEFAULT_HABITS, {}, inp=lambda _: next(answers), out=io.StringIO())
    assert marks == {"stretch": True, "protein": False, "late-caffeine": True} and note == "slept ok"


def test_custom_habits_from_config():
    hs = J.habits({"habits": ["sauna", {"key": "cold", "label": "cold plunge", "good": True}]})
    assert [h["key"] for h in hs] == ["sauna", "cold"] and hs[1]["good"] is True
    assert J.habits({}) == J.DEFAULT_HABITS


def test_load_ignores_junk(tmp_path):
    (tmp_path / "journal.json").write_text('{"days": {"bad": {}, "2026-10-01": {"habits": {"a": "yes", "b": true}}}}')
    assert J.load(tmp_path) == {"2026-10-01": {"habits": {"b": True}, "note": ""}}


@pytest.fixture
def journaled(tmp_path, monkeypatch):
    rnd = random.Random(5)
    days = {}
    for i in range(1, 60):
        d = (fixture_db.FIXTURE_DAY - timedelta(days=i)).isoformat()
        days[d] = {"habits": {h["key"]: rnd.random() < 0.4 for h in J.DEFAULT_HABITS}, "note": ""}
    days[(fixture_db.FIXTURE_DAY - timedelta(days=1)).isoformat()]["note"] = "late dinner"
    J.save(tmp_path, days)
    return _model(tmp_path, monkeypatch)


def test_journal_model(journaled):
    j = journaled["journal"]
    assert j["has_log"] and j["logged7"] == 6 and j["streak"] == 59 and j["yesterday_logged"]
    assert len(j["rows"]) == len(J.DEFAULT_HABITS) and len(j["rows"][0]["cells"]) == 14
    assert j["last_note"][1] == "late dinner"
    keys = {e["key"] for e in journaled["insights"]["effects"]}
    assert any(k.startswith("habit:") for k in keys)                  # habits feed the insights
    for e in journaled["insights"]["effects"]:
        if e["key"].startswith("habit:"):
            assert e["source"] == "journal" and e["n_yes"] >= 5 and e["n_no"] >= 5


@pytest.mark.parametrize("width", WIDTHS)
def test_journal_box_fits(journaled, width):
    buf = io.StringIO()
    dashboard.render(journaled, dashboard.make_console(width, no_color=True, file=buf), width, "journal")
    lines = buf.getvalue().splitlines()
    _check(lines, width)
    assert "days journaled" in buf.getvalue() and "late dinner" in buf.getvalue()


def test_journal_box_empty_explains(tmp_path, monkeypatch):
    m = _model(tmp_path, monkeypatch)
    buf = io.StringIO()
    dashboard.render(m, dashboard.make_console(80, no_color=True, file=buf), 80, "journal")
    assert "fitdash journal" in buf.getvalue()


def test_cli_entry_point(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    assert dashboard.main(["journal", "today", "+protein"]) == 0
    assert dashboard.main(["j", "habits"]) == 0
    assert "knee-pain" in capsys.readouterr().out
    assert json.loads((tmp_path / "journal.json").read_text())["days"]


def test_box_groups_do_and_avoid(journaled):
    buf = io.StringIO()
    dashboard.render(journaled, dashboard.make_console(100, no_color=True, file=buf), 100, "journal")
    text = buf.getvalue()
    assert text.index("TO DO") < text.index("stretching") < text.index("TO AVOID") < text.index("alcohol")
    assert "slips in the last 7 days" in text and "TRACKING" in text       # creatine is neutral by default


def test_box_colors_mean_on_track(tmp_path, monkeypatch):
    # alcohol is a habit to avoid, stretch one to do: skipping alcohol and stretching are both "on track" (green),
    # drinking and not stretching are both "off track" (red)
    J.save(tmp_path, {"2026-10-07": {"habits": {"alcohol": False, "stretch": True}, "note": ""},
                      "2026-10-08": {"habits": {"alcohol": True, "stretch": False}, "note": ""}})
    m = _model(tmp_path, monkeypatch)
    buf = io.StringIO()
    console = dashboard.make_console(100, no_color=False, file=buf, force_terminal=True)
    dashboard.render(m, console, 100, "journal")
    green, red = "38;2;12;163;12", "38;2;208;59;59"
    for label in ("stretching", "alcohol"):
        line = next(l for l in buf.getvalue().splitlines() if label in l)
        cells = line.split(label, 1)[1]
        assert cells.index(green) < cells.index(red), label         # yesterday on track, today off track

def test_ask_prints_group_headers(tmp_path):
    out = io.StringIO()
    J.ask(J.DEFAULT_HABITS, {}, inp=lambda _: "", out=out)
    assert out.getvalue().index("TO DO") < out.getvalue().index("TO AVOID")


def test_on_track_chart_uses_a_fixed_scale(journaled):
    buf = io.StringIO()
    dashboard.render(journaled, dashboard.make_console(100, no_color=True, file=buf), 100, "journal")
    text = buf.getvalue()
    assert "ON TRACK · 30 DAYS" in text and "100% ┤" in text and "  0% ┤" in text and "on logged days" in text
