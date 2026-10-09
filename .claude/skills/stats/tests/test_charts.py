import charts as K


def _plain(lines):
    return [ln.plain for ln in lines]


def test_columns_label_every_bar_when_they_fit():
    rows = _plain(K.columns([3, 12, None, 7], 4, 2, 1, "#ffffff", top=21, value_fmt=lambda v: "{:.0f}".format(v)))
    text = "\n".join(rows)
    for label in ("3", "12", "7"):
        assert label in text
    assert len(rows) == 5                       # 4 bar rows + 1 label row
    assert "·" in rows[-1]                      # the missing day is a dot, not a zero bar
    assert "┤" not in text                      # no competing axis numbers when bars are labeled


def test_columns_label_only_today_when_crowded():
    vals = [10.4, 12.7, 9.9, 15.2]
    rows = _plain(K.columns(vals, 4, 1, 0, "#ffffff", top=21, value_fmt=lambda v: "{:.1f}".format(v)))
    text = "\n".join(rows)
    assert "15.2" in text and "10.4" not in text and "12.7" not in text
    assert rows[0].rstrip().endswith("15.2")    # today's value sits on the label row, above everything
    assert "┤" in text                           # axis numbers come back when bars aren't all labeled


def test_trend_chart_marks_todays_value():
    days = ["2026-10-0{}".format(i) for i in range(1, 8)]
    lines = _plain(K.trend_chart([80, 82, None, 85, 90, 88, 91], days, 60, 3, "#ffffff",
                                 lambda v: "{:.0f}".format(v), {"mean": 84, "sd": 4}))
    assert sum("◂ 91 today" in ln for ln in lines) == 1
    assert all(len(ln) <= 60 for ln in lines)
    stale = _plain(K.trend_chart([80, 82, 91, None], days[:4], 60, 3, "#ffffff", lambda v: "{:.0f}".format(v)))
    assert any("◂ 91 last" in ln for ln in stale)   # a missing today is never shown as today


def test_ring_number_stays_inside_the_ring():
    for value in ("9.9", "12", "21"):
        lines = _plain(K.ring(0.6, "#ffffff", value, "of 21"))
        digits_rows = [ln for ln in lines if any(ch in ln for ch in "█▀▄")]
        for ln in digits_rows:
            core = ln.strip()
            # braille ring cells must still bracket the digits on every digit row
            assert 0x2800 <= ord(core[0]) <= 0x28FF and 0x2800 <= ord(core[-1]) <= 0x28FF, (value, ln)


def test_todays_bar_label_keeps_its_decimal():
    rows = _plain(K.columns([12, 7, 1.5], 4, 2, 1, "#ffffff", top=21, value_fmt=lambda v: "{:.0f}".format(v),
                            today_fmt=lambda v: "{:.1f}".format(v)))
    assert "1.5" in "\n".join(rows)


def test_timing_chart_never_wraps_an_early_bedtime():
    nights = [{"date": "2026-10-01", "start": "2026-10-01T20:30:00", "end": "2026-10-02T05:00:00", "asleep": 480}]
    row = _plain(K.timing_chart(nights, 70, []))[0]
    assert row.count("█") > 10          # a real bar from the left edge, not a single cell at the far right


def test_bed_wake_hours_colored_by_length():
    import charts as K
    nights = [{"date": "2026-10-0{}".format(i), "start": "2026-10-0{}T01:00:00".format(i), "end": "2026-10-0{}T{:02d}:00:00".format(i, 1 + h),
               "asleep": h * 60} for i, h in ((1, 8), (2, 6), (3, 4))]
    rows = K.timing_chart(nights, 80, [])
    styles = [str(r.spans[-1].style) for r in rows[:3]]
    assert K.C["good"] in styles[0] and K.C["watch"] in styles[1] and K.C["flag"] in styles[2]
