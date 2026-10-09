"""Daily top-3 priorities. Synthetic data only; the coach CLI is never called."""
import io
import json
from datetime import date, datetime

import pytest

import coach as CO
import dashboard
import data as D
import priorities as PR
from test_render import WIDTHS, _check, _model


def at(h, mi=0, day=8):
    return datetime(2026, 10, day, h, mi, tzinfo=D.NY)


def test_plan_day_rolls_over_at_4am():
    assert PR.plan_day(at(2, day=9)) == date(2026, 10, 8)
    assert PR.plan_day(at(9, day=9)) == date(2026, 10, 9)


def test_set_mark_reflect(tmp_path):
    d = date(2026, 10, 8)
    e = PR.set_items(tmp_path, d, ["  write   report ", "gym", "call mom"], at(8))
    assert [i["text"] for i in e["items"]] == ["write report", "gym", "call mom"] and e["set_at"] == "2026-10-08T08:00"
    with pytest.raises(PR.PriorityError):
        PR.set_items(tmp_path, d, ["a", "b", "c", "d"], at(8))
    with pytest.raises(PR.PriorityError):
        PR.set_items(tmp_path, d, ["", " "], at(8))
    PR.mark(tmp_path, d, 2, "done", at(20))
    PR.set_items(tmp_path, d, ["write report", "gym", "read"], at(12))         # editing keeps known statuses
    e = PR.load(tmp_path)["2026-10-08"]
    assert [i["status"] for i in e["items"]] == [None, "done", None]
    PR.mark(tmp_path, d, 1, "partial", at(21))
    PR.mark(tmp_path, d, 3, "missed", at(21))
    e = PR.load(tmp_path)["2026-10-08"]
    assert e["reviewed_at"] == "2026-10-08T21:00"
    with pytest.raises(PR.PriorityError):
        PR.mark(tmp_path, d, 4, "done", at(21))
    with pytest.raises(PR.PriorityError):
        PR.mark(tmp_path, d, 1, "maybe", at(21))
    PR.reflect(tmp_path, d, " phone   away helped ", at(22))
    assert PR.load(tmp_path)["2026-10-08"]["reflection"] == "phone away helped"


def test_summary_and_streak(tmp_path):
    for k in (5, 6, 7, 8):
        PR.set_items(tmp_path, date(2026, 10, k), ["a", "b"], at(8, day=k))
        PR.mark(tmp_path, date(2026, 10, k), 1, "done", at(20, day=k))
    PR.mark(tmp_path, date(2026, 10, 8), 2, "partial", at(20))
    s = PR.summary(PR.load(tmp_path), date(2026, 10, 8))
    assert s["set_days"] == 4 and s["items"] == 8 and s["done"] == 4 and s["partial"] == 1 and s["open"] == 3
    assert s["streak"] == 4
    assert PR.summary(PR.load(tmp_path), date(2026, 10, 9))["streak"] == 4      # today not set yet: no break


def test_due_prompt_windows(tmp_path):
    assert PR.due_prompt({}, at(9)) == "set"
    assert PR.due_prompt({}, at(15)) is None                                      # the morning prompt ends at 14:00
    assert PR.due_prompt({}, at(3)) is None
    PR.flag(tmp_path, date(2026, 10, 8), "prompt_skipped")
    assert PR.due_prompt(PR.load(tmp_path), at(10)) is None                       # skipped: don't ask again today
    PR.set_items(tmp_path, date(2026, 10, 8), ["a"], at(10))
    days = PR.load(tmp_path)
    assert PR.due_prompt(days, at(18)) is None and PR.due_prompt(days, at(20)) == "review"
    assert PR.due_prompt(days, at(1, day=9)) == "review"                          # after midnight: still last night
    PR.mark(tmp_path, date(2026, 10, 8), 1, "done", at(20))
    assert PR.due_prompt(PR.load(tmp_path), at(21)) is None


def test_interactive_set_and_review(tmp_path):
    answers = iter(["ship report", "pull day", ""])
    PR.run_prompt(tmp_path, at(9), "set", inp=lambda _: next(answers), out=io.StringIO())
    assert [i["text"] for i in PR.load(tmp_path)["2026-10-08"]["items"]] == ["ship report", "pull day"]
    answers = iter(["d", "x", "s", "started late"])                              # "x" is re-asked
    PR.run_prompt(tmp_path, at(21), "review", inp=lambda _: next(answers), out=io.StringIO())
    e = PR.load(tmp_path)["2026-10-08"]
    assert [i["status"] for i in e["items"]] == ["done", "partial"] and e["reflection"] == "started late"
    PR.run_prompt(tmp_path, at(9, day=9), "set", inp=lambda _: "", out=io.StringIO())
    assert PR.load(tmp_path)["2026-10-09"]["prompt_skipped"]


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    assert dashboard.main(["priorities", "set", "a", "b", "c"]) == 0
    assert dashboard.main(["p", "done", "1"]) == 0
    assert dashboard.main(["priorities", "some", "9"]) == 2
    assert dashboard.main(["priorities", "reflect", "good", "day"]) == 0
    capsys.readouterr()
    assert dashboard.main(["priorities", "show"]) == 0
    out = capsys.readouterr().out
    assert "● 1. a  (done)" in out and "reflection: good day" in out


@pytest.fixture
def pmodel(tmp_path, monkeypatch):
    PR.set_items(tmp_path, date(2026, 10, 8), ["Finish the chapter", "Pull day", "Lights out by 12:45"], at(8))
    PR.mark(tmp_path, date(2026, 10, 8), 2, "done", at(11))
    return _model(tmp_path, monkeypatch)


@pytest.mark.parametrize("width", WIDTHS)
def test_today_box_shows_priorities(pmodel, width):
    buf = io.StringIO()
    dashboard.render(pmodel, dashboard.make_console(width, no_color=True, file=buf), width, "today")
    _check(buf.getvalue().splitlines(), width)
    text = buf.getvalue()
    assert "PRIORITIES" in text and "Finish the chapter" in text and "● 2. Pull day · done" in text


def test_today_box_asks_when_none(tmp_path, monkeypatch):
    m = _model(tmp_path, monkeypatch)
    buf = io.StringIO()
    dashboard.render(m, dashboard.make_console(100, no_color=True, file=buf), 100, "today")
    assert "What are today's top 3?" in buf.getvalue()


def test_coach_midday_and_context(pmodel):
    m = dict(pmodel, has_data=True)
    assert CO.due(m, [], at(14)) == ("midday", None)
    done_mid = [{"date": "2026-10-08", "at": "2026-10-08T13:30", "kind": "midday", "text": "x"}]
    assert CO.due(m, done_mid, at(15)) is None
    ctx = CO.context(pmodel, "midday", None, at(14))
    assert ctx["priorities"]["today"][1] == {"text": "Pull day", "status": "done"}
    assert ctx["priorities"]["today"][0]["status"] == "open"
    assert "priorities" in CO.SYSTEM


def test_journal_section_shows_priority_history(pmodel):
    buf = io.StringIO()
    dashboard.render(pmodel, dashboard.make_console(100, no_color=True, file=buf), 100, "journal")
    assert "Priorities · 7 days" in buf.getvalue()
