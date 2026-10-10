"""Extended single-section views: `fitdash --section X` shows the normal box, then these.

Each function returns [(title, lines, border color)] panels built only from the model (mostly
`model["deep"]`), following the same hierarchy as every other box: a headline, a chart, quiet
details. Everything fits any width from 56 to 96 inner columns.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

from rich.text import Text

import charts as K
import scores as S
from charts import C, T, fit

# shared helpers from the main renderer (imported lazily by dashboard.render, so no cycle at import)
import dashboard as DB

hm, sdate, blank, headline, details, sub, MUSCLE_NAMES = DB.hm, DB.sdate, DB.blank, DB.headline, DB.details, DB.sub, DB.MUSCLE_NAMES
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _d(iso: str) -> date:
    return date.fromisoformat(iso[:10])


def _mins(iso: str | None) -> float | None:
    if not iso:
        return None
    t = datetime.fromisoformat(iso[:19])
    return t.hour * 60 + t.minute


def _night_min(iso: str | None) -> float | None:
    """Clock minutes on a night axis: 18:00 → -360, 00:30 → 30, 09:00 → 540."""
    m_ = _mins(iso)
    return None if m_ is None else m_ - 1440 if m_ >= 18 * 60 else m_


def _clock(minutes: float | None) -> str:
    if minutes is None:
        return "—"
    t = int(round(minutes)) % 1440
    h, mm = divmod(t, 60)
    return "{}:{:02d}{}".format(h % 12 or 12, mm, "a" if h < 12 else "p")


def _dur(hours: float) -> str:
    return "{}d {}h".format(int(hours // 24), int(round(hours % 24))) if hours >= 48 else "{:.0f}h".format(hours) if hours >= 1 else "<1h"


def _score_color(v: float | None) -> str:
    return C["none"] if v is None else C["good"] if v >= 90 else C["watch"] if v >= 70 else C["flag"]


def _rec_color(v: float | None) -> str:
    return C["none"] if v is None else C["recovery"]["green" if v >= 67 else "yellow" if v >= 34 else "red"]


def _legend(items: list[tuple[str, str, str]], w: int) -> list[Text]:
    return DB._flow([T((g + " ", c), (label, C["muted"])) for g, c, label in items], w, gap=3, indent=2)


# ---------------------------------------------------------------- plots

def scatter(points: list[tuple[float, float, str]], w: int, h: int, xr: tuple[float, float],
            yr: tuple[float, float], x_label: str, y_fmt) -> list[Text]:
    """A dot plot on a character grid: • one point, ● two or more in the same cell."""
    plot_w = w - 6
    grid: dict[tuple[int, int], list[str]] = {}
    for x, y, col in points:
        cx = int(round((min(max(x, xr[0]), xr[1]) - xr[0]) / (xr[1] - xr[0]) * (plot_w - 1)))
        cy = int(round((1 - (min(max(y, yr[0]), yr[1]) - yr[0]) / (yr[1] - yr[0])) * (h - 1)))
        grid.setdefault((cy, cx), []).append(col)
    out = []
    for r in range(h):
        label = y_fmt(yr[1]) if r == 0 else y_fmt(yr[0]) if r == h - 1 else ""
        line = T(("{:>4} ".format(label), C["muted"]), ("│", C["rule"]))
        for c in range(plot_w):
            cell = grid.get((r, c))
            if cell:
                line.append("●" if len(cell) > 1 else "•", style=max(set(cell), key=cell.count))
            else:
                line.append(" ")
        out.append(fit(line, w))
    out.append(fit(T(("     └" + "─" * plot_w, C["rule"])), w))
    lo, hi = "{:g}".format(xr[0]), "{:g}".format(xr[1])
    mid = x_label.center(max(0, plot_w - len(lo) - len(hi)))
    out.append(fit(T(("      " + lo + mid + hi, C["muted"])), w))
    return out


def dot_strip(vals: list[float], w: int, lo: float, hi: float, color: str) -> Text:
    cells = [0] * w
    for v in vals:
        cells[int(round((min(max(v, lo), hi) - lo) / (hi - lo) * (w - 1)))] += 1
    t = Text(no_wrap=True)
    for n in cells:
        t.append("·" if n == 0 else "•" if n == 1 else "●", style=C["faint"] if n == 0 else color)
    return t


# ---------------------------------------------------------------- sleep

def deep_sleep(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    nights = [n for n in (m.get("deep") or {}).get("nights", [])]
    have = [n for n in nights if n["asleep"] is not None]
    if len(have) < 3:
        return []
    panels = []

    # 1. score and hours vs need over the whole window
    scores = [n["score"] for n in nights]
    hrs = [None if n["asleep"] is None else n["asleep"] / 60 for n in nights]
    need = [n["need"] / 60 for n in nights if n["need"]]
    avg_need = statistics.fmean(need) if need else 8
    met = sum(1 for n in have if n["need"] and n["asleep"] >= n["need"] * 0.95)
    lines = headline(w, "good" if met >= len(have) * 0.6 else "watch" if met >= len(have) * 0.3 else "flag",
                     ("{}/{}".format(met, len(have)), "nights met your need"),
                     ("{:.0f}".format(statistics.fmean([s for s in scores if s is not None] or [0])), "avg sleep score"))
    lines.append(blank())
    lines.append(sub("Hours asleep · dotted line = your average need {}".format(hm(avg_need * 60)), w))
    col_w = max(1, min(2, (w - 8) // max(1, len(hrs)) - 1))
    gap = 1 if (col_w + 1) * len(hrs) + 7 <= w else 0
    lines += K.columns(hrs[-((w - 7) // (col_w + gap)):], 6, col_w, gap,
                       lambda v: C["sleep"] if v >= avg_need * 0.95 else C["deep"], goal=avg_need,
                       fmt=lambda v: "{:.0f}h".format(v), today_fmt=lambda v: hm(v * 60))
    lines.append(blank())
    lines.append(sub("Sleep score", w))
    lines += K.trend_chart(scores, [n["date"] for n in nights], w, 5, C["sleep"], lambda v: "{:.0f}".format(v),
                           zone_color=_score_color)
    panels.append(("Sleep · {} nights".format(len(nights)), lines, "sleep"))

    # 2. stages night by night (last 14): bar length = time asleep + awake, so long nights look long
    recent = [n for n in nights[-14:] if n["stages"]]
    if recent:
        tot = max(sum(v or 0 for v in n["stages"].values()) for n in recent) or 1
        bar_w = max(10, w - 26)
        lines = []
        rest_pct = [100 * ((n["stages"]["deep"] or 0) + (n["stages"]["rem"] or 0)) / max(1, n["asleep"] or 1) for n in recent]
        lines += headline(w, "good" if statistics.fmean(rest_pct) >= 40 else "watch",
                          ("{:.0f}%".format(statistics.fmean(rest_pct)), "restorative (deep + REM) on average"))
        lines.append(blank())
        for n in recent:
            st = n["stages"]
            total = sum(v or 0 for v in st.values())
            width = max(1, round(bar_w * total / tot))
            bar = K.segbar([(st["deep"] or 0, "█", C["deep"]), (st["rem"] or 0, "▒", C["rem"]),
                            (st["light"] or 0, "▓", C["light"]), (st["awake"] or 0, "░", C["awake"])], width)
            pct = 100 * ((st["deep"] or 0) + (st["rem"] or 0)) / max(1, n["asleep"] or 1)
            lines.append(fit(T(("  {} ".format(sdate(n["date"])[:3]), C["muted"]), bar, " " * (bar_w - width),
                               ("  {:>5}".format(hm(n["asleep"])), "bold " + C["ink"]), ("  {:>3.0f}%".format(pct), C["muted"])), w))
        lines.append(blank())
        lines += _legend([("█", C["deep"], "deep"), ("▒", C["rem"], "REM"), ("▓", C["light"], "light"),
                          ("░", C["awake"], "awake"), ("%", C["muted"], "deep + REM share")], w)
        panels.append(("Stages · night by night", lines, "sleep"))

    # 3. timing: averages, spread, weekday vs weekend
    onsets = [(n, _night_min(n["start"])) for n in have if n["start"]]
    wakes = [(n, _mins(n["end"])) for n in have if n["end"]]
    if len(onsets) >= 5:
        on_v = [v for _, v in onsets]
        wk_v = [v for _, v in wakes]
        on_sd, wk_sd = statistics.pstdev(on_v), statistics.pstdev(wk_v)
        weekend = [v for n, v in onsets if _d(n["date"]).weekday() in (5, 6)]          # nights into Sat/Sun
        weekday = [v for n, v in onsets if _d(n["date"]).weekday() not in (5, 6)]
        lines = headline(w, "good" if on_sd <= 45 else "watch" if on_sd <= 75 else "flag",
                         ("±{}".format(hm(on_sd)), "bedtime spread"), ("±{}".format(hm(wk_sd)), "wake spread"))
        lines.append(blank())
        stats = [("Avg asleep at", _clock(statistics.fmean(on_v))), ("Avg wake", _clock(statistics.fmean(wk_v))),
                 ("Earliest / latest bed", "{} / {}".format(_clock(min(on_v)), _clock(max(on_v)))),
                 ("Earliest / latest wake", "{} / {}".format(_clock(min(wk_v)), _clock(max(wk_v))))]
        if weekend and weekday:
            jet = statistics.fmean(weekend) - statistics.fmean(weekday)
            stats.append(("Weekend vs weekday bed", "{}{} ".format("+" if jet >= 0 else "−", hm(abs(jet))) +
                          ("later" if jet >= 0 else "earlier")))
        for k, v in stats:
            lines.append(fit(T(("  {:<24}".format(k), C["ink2"]), (v, "bold " + C["ink"])), w))
        lines.append(blank())
        lines.append(sub("Asleep-at time, night by night (earlier is higher)", w))
        lines += K.trend_chart([None if v is None else -v for v in [_night_min(n["start"]) for n in nights]],
                               [n["date"] for n in nights], w, 4, C["sleep"], lambda v: _clock(-v))
        lines += K.para("Spread is the standard deviation: within ±45 min counts as regular. Irregular "
                        "timing is what holds your consistency score down.", w, C["muted"], indent=2, prefix=Text("  "))
        panels.append(("Timing · {} nights".format(len(onsets)), lines, "sleep"))

    # 4. sleep debt balance over time
    debts = [n["debt"] for n in nights[-28:]]
    if any(d_ is not None for d_ in debts):
        now_debt = next((d_ for d_ in reversed(debts) if d_ is not None), 0)
        lines = headline(w, "good" if now_debt < 60 else "watch" if now_debt < 180 else "flag",
                         (hm(now_debt), "sleep debt going into last night"))
        lines.append(blank())
        lines += K.columns([None if d_ is None else d_ / 60 for d_ in debts], 5, 1, 1 if 2 * len(debts) + 7 <= w else 0,
                           lambda v: C["good"] if v < 1 else C["watch"] if v < 3 else C["flag"],
                           fmt=lambda v: "{:.0f}h".format(v), today_fmt=lambda v: hm(v * 60))
        lines += K.para("A running 7-night balance: short nights add, long nights pay back, old debt fades "
                        "15 % a night.", w, C["muted"], indent=2, prefix=Text("  "))
        panels.append(("Sleep debt · 28 nights", lines, "sleep"))

    # 5. best and worst nights
    ranked = sorted((n for n in have if n["score"] is not None), key=lambda n: n["score"])
    if len(ranked) >= 6:
        lines = []
        for title, group, lvl in (("Best", ranked[-3:][::-1], "good"), ("Worst", ranked[:3], "flag")):
            lines.append(fit(T(("  " + title, "bold " + C[lvl])), w))
            for n in group:
                lines.append(fit(T(("    {:<10}".format(sdate(n["date"])), C["ink2"]), ("{:>3}".format(n["score"]), "bold " + C["ink"]),
                                   ("   {}  {} → {}".format(hm(n["asleep"]), _clock(_mins(n["start"])), _clock(_mins(n["end"]))), C["muted"])), w))
        panels.append(("Best & worst nights", lines, "sleep"))
    return panels


# ---------------------------------------------------------------- recovery

def deep_recovery(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    daily = (m.get("deep") or {}).get("daily", [])
    if not daily:
        return []
    panels = []
    # 1. calendar: one row per week, Monday first
    by = {d_["date"]: d_["recovery"] for d_ in daily}
    last = _d(daily[-1]["date"])
    first_monday = _d(daily[0]["date"]) - timedelta(days=_d(daily[0]["date"]).weekday())
    cell = max(4, min(8, (w - 10) // 7))
    lines = []
    vals = [v for v in by.values() if v is not None]
    if vals:
        greens = sum(1 for v in vals if v >= 67)
        reds = sum(1 for v in vals if v <= 33)
        lines += headline(w, "good" if greens >= reds else "watch", ("{:.0f}%".format(statistics.fmean(vals)), "average"),
                          ("{}".format(greens), "green days"), ("{}".format(reds), "red days"))
        lines.append(blank())
    lines.append(fit(T(("  " + " " * 8, ""), *[(wd.center(cell), C["muted"]) for wd in WEEKDAYS]), w))
    wk = first_monday
    while wk <= last:
        row = T(("  {:<8}".format(sdate(wk.isoformat())[4:] if len(sdate(wk.isoformat())) > 4 else ""), C["muted"]))
        for i in range(7):
            d_ = wk + timedelta(days=i)
            v = by.get(d_.isoformat())
            if d_ > last or d_.isoformat() not in by:
                row.append(" " * cell)
            elif v is None:
                row.append("·".center(cell), style=C["faint"])
            else:
                glyph = "●" if v >= 67 else "◐" if v >= 34 else "○"
                row.append(("{} {}".format(glyph, v)).center(cell), style="bold " + _rec_color(v) if d_ == last else _rec_color(v))
        lines.append(fit(row, w))
        wk += timedelta(days=7)
    lines.append(blank())
    lines += _legend([("●", C["recovery"]["green"], "green ≥ 67"), ("◐", C["recovery"]["yellow"], "yellow"),
                      ("○", C["recovery"]["red"], "red ≤ 33"), ("·", C["faint"], "no score")], w)
    panels.append(("Recovery calendar", lines, "good"))

    # 2. recovery vs the previous day's strain
    pts = []
    for prev, cur in zip(daily, daily[1:]):
        if prev["strain"] is not None and cur["recovery"] is not None:
            pts.append((prev["strain"], cur["recovery"], _rec_color(cur["recovery"])))
    if len(pts) >= 8:
        lines = []
        hard = [y for x, y, _ in pts if x >= 13]
        easy = [y for x, y, _ in pts if x < 13]
        if hard and easy:
            lines += headline(w, "watch" if statistics.fmean(hard) < statistics.fmean(easy) - 5 else "good",
                              ("{:.0f}% vs {:.0f}%".format(statistics.fmean(hard), statistics.fmean(easy)),
                               "recovery after strain ≥ 13 vs lighter days"))
            lines.append(blank())
        lines += scatter(pts, w, 10, (0, 21), (0, 100), "previous day's strain", lambda v: "{:.0f}".format(v))
        lines += K.para("Each dot is a morning. Down and to the right means hard days cost you recovery.",
                        w, C["muted"], indent=2, prefix=Text("  "))
        panels.append(("Recovery vs yesterday's strain", lines, "good"))
    return panels


# ---------------------------------------------------------------- strain & workouts

def deep_strain(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    dp = m.get("deep") or {}
    panels = []
    # 1. activity by hour, last 7 days
    hours = dp.get("hours") or []
    if any(h["hours"] for h in hours):
        cell = 2 if w >= 6 + 48 + 2 else 1
        top = max((v or 0) for h in hours for v in (h["hours"] or [])) or 1
        lines = [fit(T(("  " + " " * 4, ""), *[((("{:<" + str(cell * 6) + "}").format(lbl)), C["muted"])
                                               for lbl in ("12a", "6a", "12p", "6p")]), w)]
        for h in hours:
            row = T(("  {:<4}".format(sdate(h["date"])[:3]), C["muted"]))
            row.append_text(K.heat_row(h["hours"] or [None] * 24, cell, top))
            lines.append(fit(row, w))
        lines += K.para("Minutes of light or more activity in each hour. Gaps (·) are hours with no data yet.",
                        w, C["muted"], indent=2, prefix=Text("  "))
        panels.append(("Activity by hour · 7 days", lines, None))

    # 2. HR zones per day + strain vs target
    daily = dp.get("daily") or []
    recent = daily[-14:]
    if any(d_["zones"] for d_ in recent):
        mx = max(sum((d_["zones"] or {}).values()) for d_ in recent) or 1
        bar_w = max(10, w - 22)
        lines = []
        tot = {k: sum((d_["zones"] or {}).get(k, 0) for d_ in recent) for k in ("fat_burn", "cardio", "peak")}
        lines += headline(w, "none", (hm(tot["cardio"] + tot["peak"]), "cardio + peak in 14 days"),
                          (hm(tot["fat_burn"]), "fat burn"))
        lines.append(blank())
        for d_ in recent:
            z = d_["zones"] or {}
            total = sum(z.values())
            width = max(0, round(bar_w * total / mx))
            bar = K.segbar([(z.get("fat_burn", 0), "▒", C["zone"]["fat_burn"]), (z.get("cardio", 0), "▓", C["zone"]["cardio"]),
                            (z.get("peak", 0), "█", C["zone"]["peak"])], width) if width else Text("")
            lines.append(fit(T(("  {} ".format(sdate(d_["date"])[:3]), C["muted"]), bar, " " * (bar_w - width),
                               ("  {:>5}".format(hm(total) if total else "—"), C["ink2"]),
                               ("  {:>4}".format("{:.1f}".format(d_["strain"]) if d_["strain"] is not None else "·"), "bold " + C["strain"][4])), w))
        lines.append(blank())
        lines += _legend([("▒", C["zone"]["fat_burn"], "fat burn"), ("▓", C["zone"]["cardio"], "cardio"),
                          ("█", C["zone"]["peak"], "peak"), ("n", C["strain"][4], "day strain")], w)
        panels.append(("Heart-rate zones · 14 days", lines, None))

    # 3. weekly totals
    if len(daily) >= 14:
        weeks = []
        last = _d(daily[-1]["date"])
        monday = last - timedelta(days=last.weekday())
        for i in range(7, -1, -1):
            ws = monday - timedelta(days=7 * i)
            days_ = [d_ for d_ in daily if ws.isoformat() <= d_["date"] <= (ws + timedelta(days=6)).isoformat()]
            if not days_:
                continue
            strains = [d_["strain"] for d_ in days_ if d_["strain"] is not None]
            weeks.append((ws, sum(strains), sum(d_["workout_min"] for d_ in days_), sum(d_["workouts"] for d_ in days_),
                          sum(d_["steps"] or 0 for d_ in days_), i == 0))
        if weeks:
            lines = [fit(T(("  {:<9}{:>9}{:>10}{:>10}{:>11}".format("week of", "strain", "training", "sessions", "steps"), C["muted"])), w)]
            top = max(x[1] for x in weeks) or 1
            for ws, st, mins, n, stp, cur in weeks:
                bar = K.hbar(st / top, w - 53, C["strain"][4 if not cur else 6]) if w - 53 >= 4 else Text("")
                lines.append(fit(T(("  {:<9}".format(sdate(ws.isoformat())[4:] + ("*" if cur else "")), C["ink2"]),
                                   ("{:>9.0f}".format(st), "bold " + C["ink"]), ("{:>10}".format(hm(mins)), C["ink2"]),
                                   ("{:>10}".format(n), C["ink2"]), ("{:>11,.0f}".format(stp), C["ink2"]), "  ", bar), w))
            lines += K.para("Strain is the sum of daily strain. * this week so far.", w, C["muted"], indent=2, prefix=Text("  "))
            panels.append(("Weekly totals · 8 weeks", lines, None))
    return panels


def deep_workouts(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    dp = m.get("deep") or {}
    wk = dp.get("workouts") or []
    if not wk:
        return []
    panels = []
    by_day: dict[str, list[dict]] = {}
    for x in wk:
        by_day.setdefault(x["date"], []).append(x)
    last = _d((dp.get("daily") or [{"date": m["date"]}])[-1]["date"])
    monday = last - timedelta(days=last.weekday())
    cell = max(4, min(9, (w - 10) // 7))
    lines = [fit(T(("  " + " " * 8, ""), *[(wd.center(cell), C["muted"]) for wd in WEEKDAYS]), w)]
    for i in range(7, -1, -1):
        ws = monday - timedelta(days=7 * i)
        row = T(("  {:<8}".format(sdate(ws.isoformat())[4:]), C["muted"]))
        for k in range(7):
            d_ = ws + timedelta(days=k)
            items = by_day.get(d_.isoformat(), [])
            if d_ > last:
                row.append(" " * cell)
            elif not items:
                row.append("·".center(cell), style=C["faint"])
            else:
                chips = Text(no_wrap=True)
                for x in items[:max(1, (cell - 1) // 2)]:
                    chips.append_text(K.chip(x["type"]))
                pad = cell - chips.cell_len
                row.append(" " * (pad // 2))
                row.append_text(chips)
                row.append(" " * (pad - pad // 2))
        lines.append(fit(row, w))
    lines.append(blank())
    types = sorted({x["type"] for x in wk}, key=lambda t: -sum(1 for x in wk if x["type"] == t))
    lines += DB._flow([K.chip(t) + Text(" " + next(x["label"] for x in wk if x["type"] == t), style=C["muted"]) for t in types], w, gap=3, indent=2)
    panels.append(("Workout calendar · 8 weeks", lines, None))

    lines = [fit(T(("  {:<14}{:>9}{:>10}{:>11}{:>10}".format("type", "sessions", "time", "avg strain", "longest"), C["muted"])), w)]
    for t in types:
        xs = [x for x in wk if x["type"] == t]
        strains = [x["strain"] for x in xs if x["strain"] is not None]
        lines.append(fit(T(("  ", ""), K.chip(t), (" {:<12}".format(xs[0]["label"][:12]), C["ink2"]),
                           ("{:>9}".format(len(xs)), "bold " + C["ink"]), ("{:>10}".format(hm(sum(x["minutes"] for x in xs))), C["ink2"]),
                           ("{:>11}".format("{:.1f}".format(statistics.fmean(strains)) if strains else "—"), C["strain"][4]),
                           ("{:>10}".format(hm(max(x["minutes"] for x in xs))), C["ink2"])), w))
    panels.append(("By activity · 8 weeks", lines, None))
    return panels


# ---------------------------------------------------------------- training, lifting, muscles

def deep_fresh(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    rows = sorted((m.get("muscles") or {}).get("rows", []), key=lambda r: (r.get("ready_in_h") or 0, r["fresh"]))
    if not rows or not (m.get("muscles") or {}).get("has_strength"):
        return []
    now = datetime.fromisoformat(m["now"]) if m.get("now") else datetime.now()
    waiting = [r for r in rows if r.get("ready_in_h")]
    lines = headline(w, "good" if not waiting else "watch",
                     ("{}/{}".format(len(rows) - len(waiting), len(rows)), "muscles at {} %+ now".format(S.READY_PCT)))
    lines.append(blank())
    bar_w = max(8, w - 44)
    horizon = max([r["ready_in_h"] or 0 for r in rows] + [24])
    for r in sorted(rows, key=lambda r: -(r.get("ready_in_h") or 0)):
        h = r.get("ready_in_h") or 0
        when = "ready now" if h <= 0 else (now + timedelta(hours=h)).strftime("%a %-I %p").replace(" AM", "a").replace(" PM", "p")
        col = S.freshness_color(r["fresh"])
        lines.append(fit(T(("  {:<11}".format(MUSCLE_NAMES[r["muscle"]]), C["ink2"]), ("{:>4}%".format(r["fresh"]), "bold " + col),
                           "  ", K.hbar(h / horizon if h else 0, bar_w, col, track="·"),
                           ("  {:<11}".format(when if h <= 0 else "in " + _dur(h)), C["ink"] if h else C["good"]),
                           (when if h > 0 else "", C["muted"])), w))
    lines += K.para("When each muscle is back to {} % if you don't train it again, from its half-life ({} h big, "
                    "{} h small) and today's recovery.".format(S.READY_PCT, S.HALF_LIFE_H["lats"], S.HALF_LIFE_H["biceps"]),
                    w, C["muted"], indent=2, prefix=Text("  "))
    return [("Ready when", lines, "activity")]


def deep_lifting(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    dp, lf = m.get("deep") or {}, m.get("lifting") or {}
    if not lf.get("has_log"):
        return []
    panels = []
    lo, hi = lf.get("target", [10, 20])

    # 1. weekly volume by group, 8 weeks: stacked bars, so balance and consistency show at a glance
    weekly = lf.get("weekly") or []
    if weekly:
        top = max(sum(x["groups"].values()) for x in weekly) or 1
        bar_w = max(10, w - 30)
        logged = [x for x in weekly if x["sets"]]
        lines = headline(w, "good" if len(logged) >= 4 else "watch",
                         ("{}/{}".format(len(logged), len(weekly)), "weeks with logged lifting"),
                         ("{:.0f}".format(statistics.fmean([x["sets"] for x in logged]) if logged else 0), "sets per logged week"))
        lines.append(blank())
        for x in weekly:
            gs = x["groups"]
            total = sum(gs.values())
            width = round(bar_w * total / top)
            bar = K.segbar([(gs[g], "█", DB.GROUP_COLOR[g]) for g in ("push", "pull", "legs", "core")], width) if width else T(("·", C["faint"]))
            lines.append(fit(T(("  {:<7}".format(sdate(x["end"])[4:]), C["muted"]), bar, " " * (bar_w - max(1, width)),
                               ("  {:>5}".format(x["sets"] if x["sets"] else "—"), "bold " + C["ink"] if total else C["muted"]),
                               ("  {:>8}".format("{:,}kg".format(x["tonnage"]) if x["tonnage"] else ""), C["muted"])), w))
        lines.append(blank())
        lines += _legend([("█", DB.GROUP_COLOR[g], g) for g in ("push", "pull", "legs", "core")] +
                         [("n", C["ink"], "sets done"), ("kg", C["muted"], "lifted")], w)
        panels.append(("Weekly volume · 8 weeks", lines, "activity"))

    # 2. the per-muscle table, once there are at least two weeks to compare
    weeks = dp.get("lift_weeks") or []
    if sum(1 for x in weeks if any(x["sets"].values())) >= 2:
        cell = max(5, min(8, (w - 14) // max(1, len(weeks))))
        lines = [fit(T(("  {:<11}".format("week ending"), C["muted"]), *[(sdate(x["end"])[4:].rjust(cell), C["muted"]) for x in weeks]), w)]
        for mu in S.MUSCLES:
            row = T(("  {:<11}".format(MUSCLE_NAMES[mu].lower()), C["ink2"]))
            for x in weeks:
                v = x["sets"].get(mu, 0)
                col = C["faint"] if v == 0 else C["good"] if lo <= v <= hi else C["watch"] if v < lo else C["strain"][4]
                row.append(("{:g}".format(v) if v else "·").rjust(cell), style=("bold " if lo <= v <= hi else "") + col)
            lines.append(fit(row, w))
        lines.append(blank())
        lines += _legend([("n", C["good"], "{}–{} sets".format(lo, hi)), ("n", C["watch"], "under"), ("n", C["strain"][4], "over")], w)
        panels.append(("Hard sets per muscle · 6 weeks", lines, "activity"))

    # 3. volume per session
    recent = (lf.get("recent_sessions") or [])[-12:]
    if recent:
        top = max(x["tonnage"] for x in recent) or 1
        bar_w = max(10, w - 36)
        lines = []
        for x in recent:
            lab = x["label"]
            col = DB.GROUP_COLOR.get(lab.split(" ")[0], DB.GROUP_COLOR["pull"])
            lines.append(fit(T(("  {:<9}".format(sdate(x["date"])), C["ink2"]), ("{:<11}".format(lab), col),
                               K.hbar(x["tonnage"] / top, bar_w, col), ("  {:>7}".format("{:,}kg".format(x["tonnage"])), "bold " + C["ink"])), w))
        lines += K.para("Weight × reps × sets per session: a rough total of the work done. Compare sessions of the "
                        "same type.", w, C["muted"], indent=2, prefix=Text("  "))
        panels.append(("Volume per session", lines, "activity"))

    # 4. strength charts for lifts done at least twice
    lifts = [r for r in lf.get("lifts", []) if len(r["points"]) >= 2][:4]
    if lifts:
        lines = []
        for r in lifts:
            vals = [p["e1rm"] for p in r["points"]]
            lines.append(fit(T(("  " + r["exercise"], "bold " + C["ink"]),
                               ("  {:.0f} → {:.0f} kg".format(vals[0], vals[-1]), C["ink2"]),
                               ("  {:+.1f}%".format(r["change_pct"]) if r["change_pct"] is not None else "", C["good"] if (r["change_pct"] or 0) > 0 else C["watch"]),
                               ("  ★ PR" if r["pr"] else "", "bold " + C["good"])), w))
            lines += K.braille_line(vals, w - 4, 3, C["strain"][4])
            lines.append(fit(T(("  " + sdate(r["points"][0]["date"]), C["muted"]),
                               (" " * max(1, w - 4 - len(sdate(r["points"][0]["date"])) - len(sdate(r["points"][-1]["date"]))), ""),
                               (sdate(r["points"][-1]["date"]), C["muted"])), w))
            lines.append(blank())
        panels.append(("Lift progress · estimated 1RM", lines, "activity"))
    else:
        panels.append(("Lift progress · estimated 1RM",
                       K.para("Strength charts appear once you've logged a lift on two different days. Log your next "
                              "{} with the same exercise names to start them.".format(
                                  ((m.get("training") or {}).get("split") or {}).get("next") or "session"),
                              w, C["muted"], indent=2, prefix=Text("  ")), "activity"))
    return panels


def deep_training(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    return deep_strain(m, w)[2:] + deep_fresh(m, w)          # weekly totals + ready-when


# ---------------------------------------------------------------- journal & insights

def deep_journal(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    dp = m.get("deep") or {}
    hs = dp.get("habits30") or []
    if not any(h["logged"] for h in hs):
        return []
    lines = []
    logged_days = sum(1 for i in range(len(dp.get("days30", []))) if any(h["cells"][i] is not None for h in hs))
    lines += headline(w, "good" if logged_days >= 21 else "watch", ("{}/30".format(logged_days), "days journaled"))
    lines.append(blank())
    label_w = min(22, max(len(h["label"]) for h in hs) + 1)
    bar_w = max(8, w - label_w - 24)          # "  " + label + bar + "  100%" + "  streak" + "  best"
    for good, title, col in ((True, "TO DO", C["good"]), (False, "TO AVOID", C["flag"]), (None, "TRACKING", C["strain"][4])):
        group = [h for h in hs if h["good"] is good]
        if not group:
            continue
        lines.append(fit(T(("  " + title.ljust(label_w + bar_w), "bold " + col), ("  rate  streak  best", C["muted"])), w))
        for h in group:
            rate = None if not h["logged"] else (h["yes"] if good is not False else h["logged"] - h["yes"]) / h["logged"]
            lines.append(fit(T(("  {:<{}}".format(DB._short(h["label"], label_w - 1), label_w), C["ink2"]),
                               K.hbar(rate, bar_w, col), ("  {:>4}".format("—" if rate is None else "{:.0f}%".format(100 * rate)), "bold " + C["ink"]),
                               ("  {:>6}".format("{}d".format(h["streak"])), C["ink2"]), ("  {:>4}".format("{}d".format(h["best_streak"])), C["muted"])), w))
        lines.append(blank())
    lines += K.para("Rate = share of logged days on the right side: did it for habits to do, skipped it for "
                    "habits to avoid. Streak = days in a row on the right side.", w, C["muted"], indent=2, prefix=Text("  "))
    return [("Habits · 30 days", lines, "sleep")]


def deep_priorities(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    import priorities as PR
    wk = (m.get("priorities") or {}).get("week") or {}
    if not wk.get("set_days"):
        return []
    rate = (wk["done"] + 0.5 * wk["partial"]) / max(1, wk["items"])
    lines = headline(w, "good" if rate >= 0.6 else "watch", ("{}/{}".format(wk["set_days"], wk["days"]), "days with priorities"),
                     ("{}/{}".format(wk["done"], wk["items"]), "done"), ("{}".format(wk["partial"]), "some progress"))
    lines.append(blank())
    col = {"done": C["good"], "partial": C["watch"], "missed": C["flag"], None: C["ink2"]}
    for d_ in wk["by_day"]:
        if not d_["items"]:
            lines.append(fit(T(("  {:<10}".format(sdate(d_["date"])), C["muted"]), ("—", C["faint"])), w))
            continue
        for k, it in enumerate(d_["items"]):
            lines.append(fit(T(("  {:<10}".format(sdate(d_["date"]) if k == 0 else ""), C["muted"]),
                               (PR.GLYPH[it.get("status")] + " ", "bold " + col[it.get("status")]),
                               (DB._short(it["text"], max(10, w - 28)), C["ink"]),
                               ("  " + PR.LABEL[it.get("status")], C["muted"])), w))
    lines.append(blank())
    lines += _legend([("●", C["good"], "done"), ("◐", C["watch"], "some progress"), ("✕", C["flag"], "not today"),
                      ("○", C["ink2"], "not reviewed")], w)
    return [("Priorities · 7 days", lines, "activity")]


def deep_insights(m: dict, w: int) -> list[tuple[str, list[Text], str]]:
    effects = [e for e in (m.get("insights") or {}).get("effects", []) if e.get("yes_vals")][:6]
    if not effects:
        return []
    lines = []
    strip_w = max(20, w - 22)            # "  without 55 " + strip + "  n=123"
    lines.append(fit(T(("  {:<12}".format(""), ""), ("0" + "next-morning Recovery".center(strip_w - 4) + "100", C["muted"])), w))
    for e in effects:
        clear = e["strength"] != "unclear"
        lines.append(fit(T(("  " + e["label"], "bold " + C["ink"] if clear else C["ink2"]),
                           ("  {:+.0f}".format(e["diff"]), "bold " + (C["good"] if e["diff"] > 0 else C["flag"]) if clear else C["muted"]),
                           ("  " + e["strength"], C["muted"])), w))
        for name, vals, mean, col in (("with", e["yes_vals"], e["mean_yes"], C["good"] if e["diff"] > 0 else C["flag"]),
                                      ("without", e["no_vals"], e["mean_no"], C["none"])):
            lines.append(fit(T(("  {:<8}{:>3} ".format(name, mean), C["muted"]), dot_strip(vals, strip_w, 0, 100, col),
                               ("  n={}".format(len(vals)), C["muted"])), w))
        lines.append(blank())
    lines += K.para("Every dot is a morning's Recovery; the number is the average. Overlapping clouds mean the "
                    "habit barely matters; clouds that sit apart mean it does. Correlation, not proof.",
                    w, C["muted"], indent=2, prefix=Text("  "))
    return [("Every morning, with vs without", lines, "good")]


DEEP = {
    "sleep": deep_sleep, "recovery": deep_recovery, "strain": deep_strain, "workouts": deep_workouts,
    "training": deep_training, "lifting": deep_lifting, "freshness": deep_fresh, "muscles": deep_fresh,
    "journal": lambda m, w: deep_priorities(m, w) + deep_journal(m, w), "insights": deep_insights,
}
