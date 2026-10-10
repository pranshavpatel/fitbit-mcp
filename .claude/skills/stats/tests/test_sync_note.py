"""The 'last night may still be syncing' warning. Synthetic data only."""
import io
from datetime import date, datetime, timedelta

import dashboard
from test_render import model  # noqa: F401  (fixture)
import data as D

DAY = date(2026, 10, 10)
MORNING = datetime(2026, 10, 10, 10, 20, tzinfo=D.NY)


def _history(today_asleep, today_end):
    nights, asleep = {}, {}
    for i in range(1, 21):
        x = (DAY - timedelta(days=i)).isoformat()
        nights[x] = {"main": {"start": x + "T01:30:00", "end": x + "T09:00:00", "minutes_asleep": 420}, "naps": []}
        asleep[x] = 420
    d = DAY.isoformat()
    nights[d] = {"main": {"start": d + "T01:36:00", "end": d + today_end, "minutes_asleep": today_asleep}, "naps": []}
    asleep[d] = today_asleep
    return nights, asleep


def test_flags_a_short_night_that_ended_early():
    nights, asleep = _history(176, "T04:32:00")                         # this morning: 2h56 ending 4:32
    note = D._night_sync_note(nights, asleep, DAY, MORNING, True)
    assert note["kind"] == "partial" and "2h56" in note["text"] and "fitdash --sync" in note["text"]


def test_no_flag_for_a_normal_or_late_ending_night():
    assert D._night_sync_note(*_history(492, "T10:02:00"), DAY, MORNING, True) is None    # full 8h12
    assert D._night_sync_note(*_history(200, "T08:30:00"), DAY, MORNING, True) is None    # short but woke at the usual time


def test_past_days_never_flag():
    assert D._night_sync_note(*_history(176, "T04:32:00"), DAY, MORNING, False) is None


def test_missing_night_in_the_morning():
    nights, asleep = _history(176, "T04:32:00")
    del nights[DAY.isoformat()]
    assert D._night_sync_note(nights, asleep, DAY, MORNING, True)["kind"] == "missing"
    assert D._night_sync_note(nights, asleep, DAY, MORNING.replace(hour=16), True) is None


def test_warning_tops_needs_attention_and_shows_in_sleep(model):
    m = dict(model)
    m["sleep"] = dict(m["sleep"], sync_note={"kind": "partial", "text": "Last night may still be syncing: test"})
    att = D.attention(m)
    assert att[0] == ["watch", "Last night may still be syncing: test"]
    m["attention"] = att
    buf = io.StringIO()
    dashboard.render(m, dashboard.make_console(100, no_color=True, file=buf), 100, "sleep")
    assert "may still be syncing" in buf.getvalue()
