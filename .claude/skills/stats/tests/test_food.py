"""The local food log. Synthetic data only."""
import io
from datetime import datetime

import pytest

import dashboard
import data as D
import food as F
from test_render import WIDTHS, _check, _model

NOW = datetime(2026, 10, 8, 20, 0, tzinfo=D.NY)


def test_parse_add():
    day, e = F.parse_add(["veggie wrap", "500", "p=20", "c=69", "f=17", "meal=lunch", "at=12:30"], NOW)
    assert day.isoformat() == "2026-10-08"
    assert e == {"name": "veggie wrap", "kcal": 500, "protein": 20.0, "carbs": 69.0, "fat": 17.0, "meal": "lunch",
                 "at": "12:30", "estimated": True}
    day, e = F.parse_add(["toast", "420", "day=yesterday"], NOW)
    assert day.isoformat() == "2026-10-07" and e["at"] == ""
    for bad in (["x"], ["x", "lots"], ["x", "9000"], ["x", "100", "p=abc"], ["x", "100", "meal=brunch"], ["x", "100", "q=1"]):
        with pytest.raises((F.FoodError, ValueError)):
            F.parse_add(bad, NOW)


def test_cli_add_show_undo(tmp_path, capsys):
    assert F.cli(["add", "banana", "105", "p=1.3", "c=27"], tmp_path, NOW) == 0
    assert F.cli(["add", "shake", "120", "p=24"], tmp_path, NOW) == 0
    capsys.readouterr()
    F.cli([], tmp_path, NOW)
    out = capsys.readouterr().out
    assert "banana" in out and "225 kcal" in out and "P 25.3g" in out
    assert F.cli(["undo"], tmp_path, NOW) == 0 and [e["name"] for e in F.load(tmp_path)["2026-10-08"]] == ["banana"]
    assert F.cli(["add", "x", "nope"], tmp_path, NOW) == 2


def test_model_uses_the_food_log_when_fitbit_has_none(tmp_path, monkeypatch):
    F.save(tmp_path, {"2026-10-08": [{"name": "wrap", "kcal": 500, "protein": 20, "carbs": 69, "fat": 17},
                                     {"name": "shake", "kcal": 120, "protein": 24}]})
    m = _model(tmp_path, monkeypatch)
    b = m["body"]
    if b["food_source"] == "fitbit":
        pytest.skip("the fixture has Fitbit food for this day")
    assert b["food_source"] == "food log" and b["cal_in_today"] == 620 and b["macros"]["protein"] == 44
    assert b["protein_target_g"][0] < b["protein_target_g"][1]


@pytest.mark.parametrize("width", WIDTHS)
def test_body_box_shows_food_today(tmp_path, monkeypatch, width):
    F.save(tmp_path, {"2026-10-08": [{"name": "wrap", "kcal": 500, "protein": 20, "carbs": 69, "fat": 17}]})
    m = _model(tmp_path, monkeypatch)
    buf = io.StringIO()
    dashboard.render(m, dashboard.make_console(width, no_color=True, file=buf), width, "body")
    _check(buf.getvalue().splitlines(), width)
    if m["body"]["food_source"] == "food log":
        assert "FOOD TODAY" in buf.getvalue() and "protein" in buf.getvalue()
