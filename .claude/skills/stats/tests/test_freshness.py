"""Muscle Freshness: the fatigue model, the dots, the icons, snapshots and the tag command."""
import io
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from rich.cells import cell_len

import dashboard
import data as D
import fixture_db
import muscle_icons as MI
import scores as S

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=D.NY)
SNAP = Path(__file__).parent / "snapshots"


# ---------------------------------------------------------------- fatigue model

def push(hours, minutes=60, strain=8.0):
    return {"hours": hours, "dose": S.session_dose(minutes, strain), "targets": S.split_targets("push (chest/tri)")}


def test_day_after_push():
    f = S.muscle_freshness([push(24)])
    assert 30 <= f["chest"] <= 70                              # primary: well into recovery, not done
    assert f["chest"] < f["triceps"] < 100                     # secondary muscles take a smaller hit
    assert f["biceps"] == f["quads"] == 100                    # untouched muscles are fully fresh


def test_day_after_legs():
    legs = {"hours": 24, "dose": S.session_dose(75, 10), "targets": S.split_targets("legs/abs")}
    f = S.muscle_freshness([legs])
    assert max(f["quads"], f["hamstrings"], f["glutes"]) < 70
    assert f["calves"] > f["quads"]                            # calves are secondary on leg day
    assert f["chest"] == 100


def test_after_a_run_legs_only_and_lighter_than_leg_day():
    run = {"hours": 24, "dose": S.session_dose(60, 12), "targets": S.activity_targets("RUNNING")}
    legs = {"hours": 24, "dose": S.session_dose(60, 12), "targets": S.split_targets("legs")}
    fr, fl = S.muscle_freshness([run]), S.muscle_freshness([legs])
    assert fr["quads"] < 100 and fr["quads"] > fl["quads"]
    assert fr["chest"] == fr["lats"] == 100
    assert S.activity_targets("WALKING") == {}


def test_no_data_and_old_sessions_are_fully_fresh():
    assert set(S.muscle_freshness([]).values()) == {100}
    assert S.muscle_freshness([push(8 * 24)])["chest"] == 100  # older than the 7-day window


def test_recovery_score_scales_the_rate():
    green = S.muscle_freshness([push(36)], S.recovery_rate(85))["chest"]
    yellow = S.muscle_freshness([push(36)], S.recovery_rate(50))["chest"]
    red = S.muscle_freshness([push(36)], S.recovery_rate(20))["chest"]
    assert green > yellow > red
    assert S.recovery_rate(None) == 1.0


def test_half_lives_and_dose_caps():
    assert S.fatigue_left(80, 48, 48) == pytest.approx(40)     # one half-life
    assert S.fatigue_left(80, 36, 36) == pytest.approx(40)
    assert S.session_dose(0, 10) == 0 and S.session_dose(600, 21) == 100
    stacked = S.muscle_freshness([push(2, 120, 15), push(1, 120, 15)])
    assert stacked["chest"] == 0                               # clamped, never negative


# ---------------------------------------------------------------- dots & colors

@pytest.mark.parametrize("pct,filled,color", [(100, 10, "#c7c7cc"), (95, 9, "#34c759"), (82, 8, "#30b350"),
                                              (60, 6, "#ffb340"), (30, 3, "#ff453a")])
def test_dot_count_and_color(pct, filled, color):
    t = dashboard.fresh_dots(pct)
    assert t.plain == "●" * 10 and cell_len(t.plain) == 10
    styles = [str(t.get_style_at_offset(dashboard.Console(), i)) for i in range(10)]
    assert all(color in st for st in styles[:filled - 1])            # filled dots use the state color
    assert "bold" in styles[filled - 1] and dashboard._tint(color) in styles[filled - 1]   # the last one glows
    assert all(dashboard.MF["off"] in st for st in styles[filled:])   # unfilled dots are dark gray
    plain = dashboard.fresh_dots(pct, color=False).plain
    assert plain.count("●") == filled and plain.count("○") == 10 - filled


def test_glyphs_are_single_width():
    for ch in "●○▀▄█░→✓!":
        assert cell_len(ch) == 1, ch


# ---------------------------------------------------------------- icons

def test_icons_are_complete_and_well_formed():
    MI.validate()
    assert set(MI.ICONS) == set(S.MUSCLES)
    for name in MI.ICONS:
        rows = MI.render(name, "#34c759")
        assert len(rows) == MI.H // 2 and all(r.cell_len == MI.W for r in rows)
        px = MI.pixels(name)
        flat = [p for row in px for p in row]
        assert any(p in "mMn" for p in flat) and "b" in flat      # a lit muscle on a gray body
        plain = "\n".join(r.plain for r in MI.render(name, "#34c759", color=False))
        assert "█" in plain and "░" in plain                    # muscle vs body still readable without color


def test_shared_torso_lights_only_the_target():
    # chest, shoulders and abs are the same front torso; only the lit region differs
    lit = {n: {(y, x) for y, row in enumerate(MI.pixels(n)) for x, p in enumerate(row) if p in "mMn"}
           for n in ("chest", "shoulders", "abs")}
    assert not (lit["chest"] & lit["shoulders"]) and not (lit["chest"] & lit["abs"])
    shape = {n: [[p != "." for p in row] for row in MI.pixels(n)] for n in ("chest", "shoulders", "abs")}
    assert shape["chest"] == shape["shoulders"] == shape["abs"]


def test_symmetric_figures_are_mirrored():
    for name in ("chest", "shoulders", "abs", "lats", "quads", "hamstrings", "glutes", "calves"):
        for row in MI.ICONS[name][0]:
            assert row == row[::-1], (name, row)


def test_muscle_shading_highlights_top_and_shadows_bottom():
    px = MI.pixels("glutes")
    col = [px[y][3] for y in range(MI.H)]
    lit = [p for p in col if p in "mMn"]
    assert lit[0] == "M" and lit[-1] == "n"


# ---------------------------------------------------------------- panel snapshots

@pytest.fixture
def model(tmp_path):
    fixture_db.create(tmp_path / "fitbit.sqlite3")
    store = D.open_store(tmp_path)
    m = D.build_model(store, fixture_db.FIXTURE_DAY, dict(D.DEFAULT_CONFIG, **fixture_db.CONFIG), now=NOW)
    store.close()
    return m


def _panel(m, width, sort="default", color=False):
    buf = io.StringIO()
    console = dashboard.make_console(width, no_color=not color, file=buf, force_terminal=color)
    console.print(dashboard.freshness_panel(m, min(width, dashboard.FRESH_MAX_W), color, sort))   # the card panel alone
    return buf.getvalue()


@pytest.mark.parametrize("width", (60, 80, 120))
def test_snapshot(model, width):
    out = _panel(model, width)
    lines = out.splitlines()
    assert all(cell_len(ln) <= width for ln in lines)
    assert lines[0].startswith("╭") and lines[0].endswith("╮")
    assert lines[-1].startswith("╰") and lines[-1].endswith("╯")
    assert all(ln.startswith("│") and ln.endswith("│") for ln in lines[1:-1])
    assert len({cell_len(ln) for ln in lines}) == 1                 # borders line up on every row
    two_cols = sum(ln.count("% recovered") for ln in lines) == 10 and any(ln.count("% recovered") == 2 for ln in lines)
    assert two_cols == (min(width, dashboard.FRESH_MAX_W) >= dashboard.FRESH_TWO_COL)
    snap = SNAP / "freshness_{}.txt".format(width)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not snap.exists():
        SNAP.mkdir(exist_ok=True)
        snap.write_text(out)
    assert out == snap.read_text()


def test_sort_by_freshness(model):
    out = _panel(model, 60, sort="freshness")
    pcts = [int(ln.split("%")[0].split()[-1]) for ln in out.splitlines() if "% recovered" in ln]
    assert pcts == sorted(pcts)


def test_color_render_uses_half_blocks_and_truecolor(model):
    out = _panel(model, 80, color=True)
    assert "\x1b[38;2;" in out and ("▀" in out or "▄" in out)


def test_no_strength_data_shows_a_message_not_numbers(tmp_path):
    fixture_db.create(tmp_path / "fitbit.sqlite3", minimal=True)
    store = D.open_store(tmp_path)
    m = D.build_model(store, fixture_db.FIXTURE_DAY, dict(D.DEFAULT_CONFIG, **fixture_db.CONFIG), now=NOW)
    store.close()
    out = _panel(m, 80)
    assert "no strength sessions logged" in out and "% recovered" not in out


# ---------------------------------------------------------------- tag command

def test_tag_writes_the_training_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    (tmp_path / "coaching").mkdir()
    (tmp_path / "coaching" / "stats.json").write_text(json.dumps({"split": fixture_db.CONFIG["split"]}))
    assert dashboard.main(["tag", "2026-10-05", "back"]) == 0
    assert json.loads((tmp_path / "training_log.json").read_text()) == {"sessions": {"2026-10-05": "back/bi"}}
    assert D.load_config(tmp_path)["lift_log"]["2026-10-05"] == "back/bi"
    assert dashboard.main(["tag", "2026-10-05", "zzz"]) == 2                 # no match, nothing written
    assert dashboard.main(["tag", "not-a-date", "back"]) == 2
