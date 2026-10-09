import io
import json
from datetime import datetime

import pytest

import data as D
import dashboard
import fixture_db
from charts import Overflow  # noqa: F401  (STATS_STRICT=1 makes any overflow raise)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=D.NY)
WIDTHS = (60, 80, 120, 140, 160, 200)


def _model(tmp_path, monkeypatch, **kw):
    db = tmp_path / "fitbit.sqlite3"
    fixture_db.create(db, **kw)
    (tmp_path / "coaching").mkdir(exist_ok=True)
    (tmp_path / "coaching" / "stats.json").write_text(json.dumps(fixture_db.CONFIG))
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    store = D.open_store(tmp_path)
    if store is None:
        return D.build_model(None, fixture_db.FIXTURE_DAY, D.load_config(tmp_path), now=NOW)
    try:
        return D.build_model(store, fixture_db.FIXTURE_DAY, D.load_config(tmp_path), now=NOW)
    finally:
        store.close()


def _render(m, width, **kw) -> list[str]:
    buf = io.StringIO()
    console = dashboard.make_console(width, no_color=True, file=buf)
    dashboard.render(m, console, width, **kw)
    return buf.getvalue().splitlines()


def _check(lines, width):
    assert lines, "nothing rendered"
    for ln in lines:
        assert len(ln) <= width, "{} > {}: {!r}".format(len(ln), width, ln)


@pytest.fixture
def model(tmp_path, monkeypatch):
    return _model(tmp_path, monkeypatch)


@pytest.mark.parametrize("width", (80, 120))      # single-column widths; two columns are tested below
def test_default_is_today_box_then_every_area(model, width):
    lines = _render(model, width)
    _check(lines, min(width, dashboard.MAX_W))
    text = "\n".join(lines)
    titles = ["Today", "Sleep", "Sleep experiment", "Recovery & heart", "Muscle Freshness", "Training plan",
              "Workouts", "Strain & activity", "This week vs last", "Body & nutrition", "Data"]
    positions = [text.index(" {} ".format(t)) for t in titles]
    assert positions == sorted(positions)                      # Today first, boxes in a fixed order
    for needle in ("RECOVERY", "NEEDS ATTENTION", "PLAN", "estimated, not WHOOP"):
        assert needle in text
    assert "--full" not in text and "--section" not in text   # no flags needed to see everything
    today_box = text[:text.index(" Sleep ")]
    assert today_box.count("\n") <= 40                         # the summary stays one screen


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("section", dashboard.SECTIONS)
def test_each_section_fits(model, width, section):
    _check(_render(model, width, section=section), width)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("period", ("week", "month"))
def test_period_views_fit(tmp_path, monkeypatch, width, period):
    db = tmp_path / "fitbit.sqlite3"
    fixture_db.create(db)
    store = D.open_store(tmp_path)
    m = D.build_model(store, fixture_db.FIXTURE_DAY, dict(D.DEFAULT_CONFIG, **fixture_db.CONFIG), now=NOW, period=period)
    store.close()
    lines = _render(m, width)
    _check(lines, width)
    assert "{} view".format(period.title()) in "\n".join(lines)
    assert any(r["metric"] == "Recovery %" and r["avg"] is not None for r in m["period"]["rows"])


def test_model_rules(model):
    # missing days stay None (never zero), and every score is bounded
    assert None in model["series"]["steps"]
    assert all(v is None or 0 <= v <= 100 for v in model["series"]["recovery"])
    assert all(v is None or 0 <= v <= 21 for v in model["series"]["strain"])
    # the duplicate WORKOUT overlapping a typed session is dropped
    days = [w["date"] for w in model["workouts"]["week"]]
    assert len(days) == len(set(days))
    # split queue advances from the anchor, experiment squares cover 14 nights
    assert model["training"]["split"]["next"] in fixture_db.CONFIG["split"]
    assert len(model["experiment"]["nights"]) == 14
    assert json.dumps(model, default=str)


@pytest.mark.parametrize("width", WIDTHS)
def test_empty_database(tmp_path, monkeypatch, width):
    m = _model(tmp_path, monkeypatch, empty=True)
    assert not m["has_data"]
    lines = _render(m, width)
    _check(lines, width)
    assert "No data to show" in "\n".join(lines)


def test_missing_database(tmp_path):
    m = D.build_model(None, fixture_db.FIXTURE_DAY, D.DEFAULT_CONFIG, now=NOW)
    assert not m["has_data"]
    _check(_render(m, 80), 80)


@pytest.mark.parametrize("width", WIDTHS)
def test_steps_only_database_shows_reasons_not_guesses(tmp_path, monkeypatch, width):
    m = _model(tmp_path, monkeypatch, minimal=True)
    assert m["recovery"]["score"] is None and m["recovery"]["reason"] == "no overnight HRV"
    assert m["sleep"]["has_night"] is False
    lines = _render(m, width)
    _check(lines, width)
    text = "\n".join(lines)
    assert "no overnight HRV" in text and "no night recorded" in text


def test_cli_json_and_no_color(tmp_path, monkeypatch, capsys):
    fixture_db.create(tmp_path / "fitbit.sqlite3")
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    assert dashboard.main(["--date", "2026-10-08", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["date"] == "2026-10-08"
    assert dashboard.main(["--date", "2026-10-08", "--width", "80", "--no-color", "--section", "sleep"]) == 0
    printed = capsys.readouterr().out
    assert "\x1b[" not in printed and "Sleep" in printed


@pytest.mark.parametrize("width", WIDTHS)
def test_long_trend_window_fits(tmp_path, width):
    fixture_db.create(tmp_path / "fitbit.sqlite3", days=120)
    store = D.open_store(tmp_path)
    m = D.build_model(store, fixture_db.FIXTURE_DAY, dict(D.DEFAULT_CONFIG, **fixture_db.CONFIG), days=90, now=NOW)
    store.close()
    assert len(m["days"]) == 90
    _check(_render(m, width), width)


@pytest.mark.parametrize("width", WIDTHS)
def test_visual_workouts_training_body(model, width):
    workouts = "\n".join(_render(model, width, section="workouts"))
    assert "Intensity this week" in workouts and "Key sessions" in workouts   # day rows, intensity split, key sessions
    assert "▚" in workouts and "lifting" in workouts                          # lifting time has its own pattern
    training = "\n".join(_render(model, width, section="training"))
    assert "▶ NEXT" in training and "┏" in training and "✓" in training     # split track, next chip highlighted
    body = "\n".join(_render(model, width, section="body"))
    assert "lean-bulk corridor" in body and "Target by today" in body and "●" in body


@pytest.mark.parametrize("width", (140, 160, 200))
def test_two_columns_when_wide(model, width):
    lines = _render(model, width)
    _check(lines, width)
    i = next(n for n, ln in enumerate(lines) if "─  Sleep  ─" in ln)    # the Sleep box's title border
    assert "Muscle Freshness" in lines[i + 1]                           # recovery side left, training side right
    text = "\n".join(lines)
    assert text.index("─  Today  ─") < text.index("─  Sleep  ─") < text.index("─  Data  ─")


def test_one_column_below_two_column_width(model):
    lines = _render(model, 120)
    assert not any(("Sleep" in ln or "─  Sleep  ─" in ln) and "Muscle Freshness" in ln for ln in lines)


def test_review_fixes_in_model(model):
    tn = model["sleep"]["tonight"]
    assert tn["asleep_by"] is not None and tn["need"]["total"] >= 300      # bedtime from need + wake anchor
    assert "tonight" in model["sleep"] and isinstance(model["low_wear_days"], list)
    hardest = max((w for w in model["workouts"]["week"] if w["strain"] is not None), key=lambda w: w["strain"])
    text = "\n".join(_render(model, 100, section="workouts"))
    assert hardest["label"] in text                                        # the headline's session is visible
    week = "\n".join(_render(model, 100, section="training"))
    assert "═" in week and "▓" in week and "1.3" in week                    # load zones readable without color


def test_low_wear_day_is_missing_not_rest(tmp_path):
    db = tmp_path / "fitbit.sqlite3"
    fixture_db.create(db)
    import sqlite3
    conn = sqlite3.connect(db)          # strip a day's sleep and activity-level minutes: the band wasn't worn
    gone = "2026-10-06"
    conn.execute("DELETE FROM records WHERE local_date=? AND metric IN ('minutes_asleep','activity_level_minutes')", (gone,))
    conn.execute("INSERT INTO records (provider, category, metric, granularity, record_key, local_date, start_local, "
                 "time_basis, value, payload_id, parser, status, fingerprint, first_seen, last_seen, updated_at) "
                 "VALUES ('google','activity','activity_level_minutes','interval','x1','2026-09-20','2026-09-20T10:00:00',"
                 "'date_only',1,NULL,'test','active','f','t','t','t')")
    conn.commit()
    conn.close()
    store = D.open_store(tmp_path)
    m = D.build_model(store, fixture_db.FIXTURE_DAY, dict(D.DEFAULT_CONFIG, **fixture_db.CONFIG), now=NOW)
    store.close()
    assert gone in m["low_wear_days"]
    assert m["series"]["strain"][m["days"].index(gone)] is None


def test_lift_labels_follow_the_rotation_and_the_log():
    w = [{"type": "STRENGTH_TRAINING", "date": d, "start": d + "T17:00:00"} for d in
         ("2026-09-28", "2026-10-01", "2026-10-03", "2026-10-06")]
    cfg = {"split": ["push", "pull", "legs", "arms"], "split_anchor": {"date": "2026-10-01", "day": "push"},
           "lift_log": {"2026-10-06": "arms"}}
    D._label_lifts(w, cfg)
    assert [x["split"] for x in w] == [None, "push", "pull", "arms"]          # before the anchor stays unknown
    assert [x["split_source"] for x in w] == [None, "logged", "assumed", "logged"]


def test_muscle_box(model):
    rows = {r["muscle"]: r for r in model["muscles"]["rows"]}
    assert set(rows) == set(D.S.MUSCLES)
    assert all(0 <= r["fresh"] <= 100 for r in rows.values())
    for width in WIDTHS:
        text = "\n".join(_render(model, width, section="freshness"))
        assert "Muscle Freshness" in text and "% recovered" in text and "●" in text
    workouts = "\n".join(_render(model, 100, section="workouts"))
    assert any("Z{}".format(z) in workouts for z in range(1, 6))


def _sgr_params(text):
    """Yield the attribute codes of every SGR escape, skipping 38;2;r;g;b / 48;2;… color payloads."""
    import re
    for seq in re.findall(r"\x1b\[([0-9;]*)m", text):
        parts = [p for p in seq.split(";") if p != ""]
        i = 0
        while i < len(parts):
            if parts[i] in ("38", "48") and i + 1 < len(parts):
                i += 5 if parts[i + 1] == "2" else 3
                continue
            yield parts[i]
            i += 1


def _contrast(hexcolor, bg=(0, 0, 0)):
    def lum(rgb):
        c = [v / 255 for v in rgb]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    rgb = tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    a, b = lum(rgb), lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_no_dim_attribute_and_readable_secondary_text(model):
    # the terminal "dim" attribute (SGR 2) halves brightness and made gray text unreadable on black
    buf = io.StringIO()
    console = dashboard.make_console(160, no_color=False, file=buf, force_terminal=True)
    dashboard.render(model, console, 160)
    codes = set(_sgr_params(buf.getvalue()))
    assert "2" not in codes
    from charts import C
    for role in ("muted", "ink2", "ink"):
        assert _contrast(C[role]) >= 4.5, role                     # WCAG AA for text on black
    assert _contrast(dashboard.MF["dim"]) >= 4.5


def test_workouts_one_row_per_day(model):
    lines = _render(model, 100, section="workouts")
    top = lines[:next(i for i, ln in enumerate(lines) if "Intensity this week" in ln)]
    day_rows = [ln for ln in top if any(ln.startswith("│  {} ".format(dn)) for dn in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))]
    assert len(day_rows) == 7                                         # the week at a glance, not a row per session
    assert sum("Key sessions" in ln for ln in lines) == 1
    key = lines[next(i for i, ln in enumerate(lines) if "Key sessions" in ln) + 1:]
    assert sum(1 for ln in key[:5] if ln.startswith("│  ") and any(ch.isdigit() for ch in ln[:12])) <= 4   # at most 4 key sessions


def test_week_vs_last_box(model):
    text = "\n".join(_render(model, 100, section="week"))
    for label in ("Recovery", "Sleep", "HRV", "Resting HR", "Training", "Lifting"):
        assert label in text
    assert "better" in text and "worse" in text


@pytest.mark.parametrize("width", (140, 160, 200))
def test_week_box_balances_the_columns(model, width):
    col = min(dashboard.MAX_W, (width - 1) // 2)
    base = [sum(dashboard._box_height(k, model, col) for k in keys) for keys in dashboard.COLUMNS]
    gap_before = abs(base[0] - base[1])
    week = dashboard._box_height("week", model, col)
    shorter = base.index(min(base))
    after = list(base)
    after[shorter] += week
    assert abs(after[0] - after[1]) <= max(gap_before, week)        # never makes the imbalance worse
    lines = _render(model, width)
    assert any("This week vs last" in ln for ln in lines)


def test_sleep_score_is_shown(model):
    sc = model["sleep"]["score"]
    assert sc and 0 <= sc["score"] <= 100 and sc["band"] in ("optimal", "sufficient", "poor")
    sleep = "\n".join(_render(model, 100, section="sleep"))
    for part in ("sleep score", "Hours vs need", "Efficiency", "Consistency", "Sleep stress"):
        assert part in sleep
    today = "\n".join(_render(model, 100, section="today"))
    assert "sleep score" in today and "Sleep score" in today


# ---------------------------------------------------------------- auto-sync decisions

class _Calls:
    def __init__(self):
        self.n = 0

    def __call__(self, *a, **k):
        self.n += 1


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    fixture_db.create(tmp_path / "fitbit.sqlite3")             # its last sync is days before "now": stale
    monkeypatch.setenv("FITBIT_MCP_HOME", str(tmp_path))
    calls = _Calls()
    import subprocess
    monkeypatch.setattr(subprocess, "run", calls)
    return calls


def _tty(monkeypatch, on):
    import sys as _sys
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: on, raising=False)


def test_auto_sync_runs_in_a_terminal_when_stale(sync_env, monkeypatch, capsys):
    _tty(monkeypatch, True)
    dashboard.main(["--section", "logs", "--no-color", "--width", "80"])
    assert sync_env.n == 1
    assert "refreshing" in capsys.readouterr().err


def test_no_auto_sync_when_piped_json_past_date_or_no_sync(sync_env, monkeypatch):
    _tty(monkeypatch, False)
    dashboard.main(["--section", "logs", "--no-color"])                       # piped (Claude, scripts)
    _tty(monkeypatch, True)
    dashboard.main(["--json"])
    dashboard.main(["--date", "2026-10-07", "--section", "logs", "--no-color"])  # a past day
    dashboard.main(["--no-sync", "--section", "logs", "--no-color"])
    assert sync_env.n == 0


def test_fresh_data_skips_sync_and_sync_flag_forces_it(sync_env, monkeypatch):
    _tty(monkeypatch, True)
    monkeypatch.setattr(dashboard, "_minutes_since_sync", lambda: 5.0)
    dashboard.main(["--section", "logs", "--no-color"])
    assert sync_env.n == 0
    _tty(monkeypatch, False)
    dashboard.main(["--sync", "--section", "logs", "--no-color"])
    assert sync_env.n == 1
