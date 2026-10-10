#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13"]
# ///
"""fitdash: all your Fitbit / Google Health stats as a terminal dashboard.

Reads ~/.fitbit-mcp/fitbit.sqlite3 read-only. Syncing (automatic when stale) is the read-only Google
sync of the fitbit-mcp project; `tag` and `lift` write only your own logs in ~/.fitbit-mcp.

  fitdash                               the Today box, then every area in its own box
                                        (syncs first if the last sync is over 30 min old)
  fitdash --sync / --no-sync            always / never sync before drawing
  fitdash --section freshness           one panel: today|sleep|recovery|insights|journal|
                                        freshness|lifting|strain|workouts|training|week|body|logs
  fitdash --section freshness --sort freshness   least-recovered muscles first
  fitdash --period week                 7- or 30-day averages vs the previous period (week|month)
  fitdash --date 2026-10-07 --days 42 --width 120 --no-color --json
  fitdash tag <YYYY-MM-DD|today|yesterday> <split day>
                                        record what a lift trained (push, pull, legs, arms, ...)
  fitdash lift [yesterday|YYYY-MM-DD] bench 3x8@60 row 4x10@50
                                        log sets (kg; 25lb for pounds; no @ = bodyweight);
                                        `fitdash lift` shows today, `fitdash lift undo` removes the last
  fitdash journal [yesterday] +stretch -alcohol note "…"
                                        daily habits + note (`fitdash journal` alone asks each one;
                                        `fitdash journal habits` lists them)
  fitdash --html [PATH]                 the dashboard as a phone-friendly web page
  fitdash app [--port 8787]             the phone app: view everything, log habits and lifts
  fitdash web --install | --status | --uninstall
                                        run the phone app in the background (this Mac only) and keep
                                        the terminal-style page fresh; reach it from your phone with
                                        Tailscale: tailscale serve --bg 8787
  fitdash priorities [set "…" "…" "…" | done 1 | some 2 | missed 3 | reflect "…" | show]
                                        today's top 3: asked in the morning, reviewed in the evening
  fitdash coach run | show | set <kind> "text" | --install | --uninstall
                                        the coach note in the Today box: written by Claude in the
                                        morning, after workouts and in the evening
  fitdash brief [--print]               sync + one notification: recovery, sleep, today's session
  fitdash brief --install [HH:MM] | --uninstall | --status
                                        run the brief every morning (launchd, this Mac only)
  fitdash --section lifting|insights|journal
                                        sets & progress · what drives your recovery · habits
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

import charts as K  # noqa: E402
import muscle_icons as MI  # noqa: E402
import data as D  # noqa: E402
import scores as S  # noqa: E402
from charts import C, T, fit  # noqa: E402

SECTIONS = ("today", "overview", "sleep", "recovery", "insights", "journal", "muscles", "freshness", "lifting", "strain", "workouts",
            "training", "week", "body", "logs")


# ---------------------------------------------------------------- formatting helpers

def hm(minutes) -> str:
    if minutes is None:
        return "—"
    h, m = divmod(int(round(minutes)), 60)
    return "{}h{:02d}".format(h, m) if h else "{}m".format(m)


def clock(ts: str | None) -> str:
    t = D.local_dt(ts)
    return "—" if t is None else t.strftime("%-I:%M %p")


def short_clock(minutes_after_midnight: float | None) -> str:
    if minutes_after_midnight is None:
        return "—"
    m = int(round(minutes_after_midnight)) % 1440
    h, mm = divmod(m, 60)
    return "{}:{:02d}{}".format((h % 12) or 12, mm, "a" if h < 12 else "p")


def sdate(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%a %-m/%-d")


def nodata(w: int, what: str = "no data") -> Text:
    return fit(T((what, C["muted"])), w)


def nodata_lines(w: int, what: str, indent: int = 2) -> list[Text]:
    return K.para(what, w, C["muted"], indent=indent)


def kv(label: str, value: str, unit: str = "", w: int = 80, label_w: int = 12, note: str = "",
       vstyle: str | None = None) -> Text:
    return K.fit_parts(w, ("{:<{lw}}".format(label, lw=label_w), C["muted"]), (value, vstyle or ("bold " + C["ink"])),
                       (" " + unit if unit else "", C["muted"]), (None, "  " + note if note else "", C["muted"]))


def blank() -> Text:
    return Text("")


def section_title(title: str, w: int, note: str = "") -> Text:
    """A section heading inside a box: UPPERCASE in bold, with a quieter note beside it."""
    return fit(T((title.upper(), "bold " + C["ink2"]), ("  " + note if note else "", C["muted"])), w)


def sub(title: str, w: int, note: str = "") -> Text:
    return K.fit_parts(w, (title, "bold " + C["ink2"]), (None, "  " + note if note else "", C["muted"]))


# ---------------------------------------------------------------- overview

def sec_overview(m: dict, w: int, header: bool = True) -> list[Text]:
    day = date.fromisoformat(m["date"])
    firsts = [m["data_since"]] if m.get("data_since") else []
    nights = sum(1 for v in m["series"]["asleep"] if v is not None)
    stamp, sync = day.strftime("%a %b %-d, %Y"), "synced {}".format(_sync_label(m))
    if not header:
        out: list[Text] = []
    elif len(stamp) + 5 + len(sync) <= w:
        out = [K.fit_parts(w, (stamp, "bold " + C["ink"]), ("  ·  " + sync, C["muted"]),
                           (None, "  ·  data since {}".format(date.fromisoformat(min(firsts)).strftime("%b %-d")) if firsts else "", C["muted"]),
                           (None, "  ·  {}/{} nights".format(nights, len(m["days"])), C["muted"])), blank()]
    else:                                       # narrow window: the sync note gets its own line
        out = [fit(T((stamp, "bold " + C["ink"])), w)] + K.para(sync, w, C["muted"]) + [blank()]

    ring_w = 20 if w >= 66 else 18          # narrow windows (≈60 cols) get slightly smaller rings
    rec, st, sl = m["recovery"], m["strain"], m["sleep"]
    rings = [
        (K.ring(None if rec["score"] is None else rec["score"] / 100,
                C["recovery"].get(rec["band"] or "", C["muted"]), "—" if rec["score"] is None else str(rec["score"]), "%", w=ring_w),
         "RECOVERY", (rec["band"] + " " + K.ICON[{"green": "good", "yellow": "watch", "red": "flag"}[rec["band"]]])
         if rec["band"] else rec["reason"] or ""),
        (K.ring(None if st["day"] is None else st["day"] / 21, K.strain_color(st["day"]),
                "—" if st["day"] is None else ("{:.0f}" if st["day"] >= 10 else "{:.1f}").format(st["day"]), "of 21", w=ring_w),
         "STRAIN" + (" so far" if m["partial_day"] else ""),
         "target {:.0f}–{:.0f}".format(*st["target"]) if st["target"] else (st["reason"] or "")),
        (K.ring(None if _sscore(sl) is None else _sscore(sl) / 100, C["sleep"],
                "—" if _sscore(sl) is None else str(_sscore(sl)), "sleep score", w=ring_w),
         "SLEEP", "{} {}".format((sl.get("score") or {}).get("band"), SLEEP_BAND_ICON[(sl.get("score") or {}).get("band")])
         if _sscore(sl) is not None else "no night recorded"),
    ]
    side = w >= 120
    block_w = 3 * ring_w + 4
    gap = 2 if side else max(1, min(2, (w - 3 * ring_w) // 2)) if w < 66 else max(2, (w - 3 * ring_w) // 4)
    lead = 0 if side else max(0, (w - 3 * ring_w - 2 * gap) // 2)
    rows = []
    for r in range(len(rings[0][0])):
        t = Text(" " * lead, no_wrap=True)
        for i, (lines, _, _) in enumerate(rings):
            if i:
                t.append(" " * gap)
            t.append_text(lines[r])
        rows.append(t)
    lab = Text(" " * lead, no_wrap=True)
    note = Text(" " * lead, no_wrap=True)
    for i, (_, title, sub_) in enumerate(rings):
        if i:
            lab.append(" " * gap)
            note.append(" " * gap)
        lab.append(title.center(ring_w)[:ring_w], style="bold " + C["ink2"])
        note.append(sub_[:ring_w].center(ring_w), style=C["muted"])
    rows += [lab, note]

    v = m["verdict"]
    right = [fit(T((K.ICON[v["level"]] + " " + v["headline"], "bold " + C[v["level"]])), w - block_w - 3 if side else w)]
    for lvl, text in v["reasons"]:
        right += K.status_lines(lvl, text, w - block_w - 3 if side else w)
    if side:
        for i in range(max(len(rows), len(right) + 1)):
            left = rows[i] if i < len(rows) else Text(" " * block_w)
            pad = block_w - left.cell_len
            line = left + Text(" " * (pad + 3))
            if 1 <= i <= len(right):
                line.append_text(right[i - 1])
            out.append(fit(line, w))
    else:
        out += [fit(r, w) for r in rows] + [blank()] + right
    return out


SLEEP_BAND_ICON = {"optimal": "✓", "sufficient": "·", "poor": "✕", None: ""}
SLEEP_BAND_LEVEL = {"optimal": "good", "sufficient": "watch", "poor": "flag", None: "none"}


def _sscore(sl: dict) -> int | None:
    return (sl.get("score") or {}).get("score")


def _sync_label(m: dict) -> str:
    if not m.get("last_sync"):
        return "never"
    t = datetime.fromisoformat(m["last_sync"])
    now = datetime.fromisoformat(m["now"]) if m.get("now") else datetime.now(D.NY)
    age = now - t
    stale = "  (stale: {:.0f}h old)".format(age.total_seconds() / 3600) if age > timedelta(hours=6) else ""
    return t.strftime("%-I:%M %p %b %-d") + stale


# ---------------------------------------------------------------- shared section pieces
#
# Every box follows the same hierarchy as the Today box:
#   1. a headline: status icon + the one or two numbers that matter, in bold
#   2. one or two charts
#   3. secondary details as a quieter "label value · label value" block at the bottom
#
# Quieter means a lower-contrast *color*, never the terminal "dim" attribute: many terminals render
# dim at half brightness, which makes gray text unreadable on a black background.

def headline(w: int, level: str, *parts: tuple[str, str]) -> list[Text]:
    """✓ VALUE label  ·  VALUE label. Values bold, labels muted; wraps whole parts if needed."""
    items = []
    for i, (value, label) in enumerate(parts):
        t = Text(no_wrap=True)
        if i == 0:
            t.append(K.ICON[level] + " ", style="bold " + C[level])
        t.append(value, style="bold " + C["ink"])
        if label:
            t.append(" " + label, style=C["ink2"])
        items.append(t)
    return _flow(items, w, gap=4, indent=2)


def takeaway(text: str, w: int) -> list[Text]:
    return K.para(text, w, C["ink2"], indent=2, prefix=Text("  "))


def details(pairs: list[tuple[str, str]], w: int) -> list[Text]:
    """Secondary facts, deliberately quiet: gray labels, lighter values, wrapped as whole pairs."""
    out, row = [], []
    for k, v in pairs:
        if v in (None, ""):
            continue
        item = T((k + " ", C["muted"]), (v, C["ink2"]))
        if item.cell_len <= w:
            row.append(item)
            continue
        out += _flow(row, w, gap=3)       # a pair too long for one line gets its own wrapped block
        row = []
        out += K.para(v, w, C["muted"], indent=2, prefix=T((k + " ", C["muted"])))
    return out + _flow(row, w, gap=3)


def _flow(items: list[Text], w: int, gap: int = 2, indent: int = 0) -> list[Text]:
    """Lay items left to right, wrapping whole items onto indented continuation lines."""
    lines, cur, base = [], Text(no_wrap=True), 0
    for it in items:
        add = it.cell_len + (gap if cur.cell_len > base else 0)
        if cur.cell_len > base and cur.cell_len + add > w:
            lines.append(fit(cur, w))
            cur, base = Text(" " * indent, no_wrap=True), indent
        if cur.cell_len > base:
            cur.append(" " * gap)
        cur.append_text(it)
    if cur.cell_len > base:
        lines.append(fit(cur, w))
    return lines


def _level(value, good, watch, higher_better=True):
    if value is None:
        return "none"
    if not higher_better:
        value, good, watch = -value, -good, -watch
    return "good" if value >= good else "watch" if value >= watch else "flag"


# ---------------------------------------------------------------- sleep

def _sleep_breakdown(sl: dict, w: int) -> list[Text]:
    """The four parts of the score, each as a 0-100 bar with its weight and what it measured."""
    comps = (sl.get("score") or {}).get("components") or {}
    nd = sl.get("need") or {}
    rows = [("sufficiency", "Hours vs need", "{} of {}".format(hm(sl.get("asleep")), hm(nd.get("total")))),
            ("efficiency", "Efficiency", "{} of {} in bed".format(hm(sl.get("asleep")), hm(sl.get("in_bed")))),
            ("consistency", "Consistency", "bed/wake, last 4 nights"),
            ("stress", "Sleep stress", "no HRV data" if sl.get("stress_pct") is None
             else "{:.0f}% of night high stress".format(sl["stress_pct"]))]
    bw = max(8, min(20, w - 58))
    out = []
    for key, label, note in rows:
        c = comps.get(key) or {}
        v = c.get("score")
        weight = "×{:.0f}%".format(100 * c["weight"]) if c.get("weight") else "  —"
        col = C["good"] if (v or 0) >= 90 else C["sleep"] if (v or 0) >= 70 else C["watch"] if (v or 0) >= 40 else C["flag"]
        out.append(K.fit_parts(w, ("  {:<14}".format(label), C["ink2"]),
                               K.hbar(None if v is None else v / 100, bw, col),
                               ("{:>5}".format("—" if v is None else v), "bold " + C["ink"]), ("  {:<5}".format(weight), C["muted"]),
                               (None, note, C["muted"])))
    out += K.para("WHOOP's four sleep components; WHOOP doesn't publish its weights, so these are ours. "
                  "≥90 optimal · 70–89 sufficient · <70 poor.", w, C["muted"], indent=2, prefix=Text("  "))
    return out


def sec_sleep(m: dict, w: int) -> list[Text]:
    sl = m["sleep"]
    out: list[Text] = []
    if not sl.get("has_night"):
        out += headline(w, "none", ("—", "no night recorded for {}".format(sdate(m["date"]))))
    else:
        sc = sl.get("score") or {}
        out += headline(w, SLEEP_BAND_LEVEL[sc.get("band")], (str(sc.get("score", "—")), "sleep score"),
                        (sc.get("band") or "—", ""), (hm(sl["asleep"]), "asleep"),
                        ("{} → {}".format(clock(sl["start"]), clock(sl["end"])), ""))
        out.append(blank())
        out.append(section_title("Score breakdown", w, "out of 100"))
        out += _sleep_breakdown(sl, w)
        out.append(blank())
        out.append(section_title("Stages", w))
        if sl["timeline"]:
            total = max(seg["end_min"] for seg in sl["timeline"])
            out += K.hypnogram(sl["timeline"], total, w, clock(sl["start"]), clock(sl["end"]))
        stg = sl["stages"]
        tot = sum((stg[k] or 0) for k in stg) or 1
        out.append(fit(K.segbar([(stg[k] or 0, K.STAGE_GLYPH[k], C[k]) for k in ("deep", "light", "rem", "awake")],
                                min(w, 72)), w))
        out += _flow([T((K.STAGE_GLYPH[k] + " ", C[k]), (name + " ", C["ink2"]),
                        ("{} {:.0f}%".format(hm(stg[k]), (stg[k] or 0) / tot * 100), C["muted"]))
                      for k, name in (("deep", "Deep"), ("light", "Light"), ("rem", "REM"), ("awake", "Awake"))], w, 3)
        out.append(blank())
        nd = sl.get("need") or {}
        out += details([
            ("Need", "{} (base {} + ½ of debt {} + strain {}{})".format(
                hm(nd.get("total")), hm(nd.get("base")), hm(nd.get("debt_adj")), hm(nd.get("strain_adj")),
                " − naps {}".format(hm(nd.get("nap_credit"))) if nd.get("nap_credit") else "")),
            ("Sleep debt going into tonight", hm((sl.get("tonight") or {}).get("debt"))),
            ("Restorative", "{} ({}%)".format(hm(sl["restorative_min"]), sl["restorative_pct"])),
            ("Awake", "{}×".format(sl["awake_count"]) if sl["awake_count"] is not None else "—"),
            ("Naps", ", ".join(hm(n["asleep"]) for n in sl.get("naps") or []) or "none"),
        ], w)
    out.append(blank())
    cfg = m["config"]
    marks, legend = [], []
    for key, glyph_color, label in (("bedtime_goal", C["good"], "goal"), ("bedtime_step", C["watch"], "step target"),
                                    ("wake_anchor", C["ink2"], "wake")):
        if cfg.get(key):
            marks.append((D.hhmm(cfg[key]), "┃", glyph_color))
            legend.append(T(("┃", glyph_color), (" {} {}".format(short_clock(D.hhmm(cfg[key])), label), C["muted"])))
    out.append(section_title("Bed & wake", w, "14 nights"))
    out += K.timing_chart(sl["timing"], w, marks)
    legend += [T(("7h+", "bold " + C["good"]), (" enough", C["muted"])), T(("5–7h", "bold " + C["watch"]), (" short", C["muted"])),
               T(("<5h", "bold " + C["flag"]), (" very short", C["muted"]))]
    out += _flow(legend, w, gap=3)
    return out


def sec_experiment(m: dict, w: int) -> list[Text]:
    """The sleep experiment night by night: when you fell asleep (green on target, red later) and how
    long you slept, in rows of up to 7 nights."""
    ex = m.get("experiment")
    if not ex:
        return nodata_lines(w, "No sleep experiment running. Set one in stats.json (\"experiment\").")
    nights = ex["nights"]
    done = [n for n in nights if n["state"] != "upcoming"]
    hits = [n for n in done if n["state"] == "hit"]
    out = headline(w, "good" if done and len(hits) >= len(done) * 0.7 else "watch" if done else "none",
                   ("{}/{}".format(len(hits), len(done)), "nights on target"), ("{}".format(len(nights) - len(done)), "to go"))
    out += takeaway(ex["name"] + ".", w)
    out.append(blank())
    cell = max(9, min(10, (w - 2) // 7))
    per_row = max(1, min(7, (w - 2) // cell))

    def at(n):
        if not n.get("start"):
            return ""
        t = D.local_dt(n["start"])
        return short_clock(t.hour * 60 + t.minute)

    for first in range(0, len(nights), per_row):
        chunk = nights[first:first + per_row]
        r1, r2, r3 = Text("  ", no_wrap=True), Text("  ", no_wrap=True), Text("  ", no_wrap=True)
        for k, n in enumerate(chunk):
            st = n["state"]
            col = C["good"] if st == "hit" else C["flag"] if st == "miss" else C["faint"]
            r1.append("night {}".format(first + k + 1).ljust(cell), style=C["muted"])
            if st == "upcoming":
                r2.append("□".ljust(cell), style=col)
            else:
                r2.append(("■ " + at(n)).ljust(cell), style="bold " + col)
            r3.append((hm(n["asleep"]) if n.get("asleep") else "").ljust(cell), style=C["muted"])
        out += [fit(r1, w), fit(r2, w)] + ([fit(r3, w)] if r3.plain.strip() else [])
        if first + per_row < len(nights):
            out.append(blank())
    out.append(blank())
    out += _flow([T(("■ ", C["good"]), ("asleep by {}".format(short_clock(D.hhmm(ex["lights_out"]))), C["muted"])),
                  T(("■ ", C["flag"]), ("later", C["muted"])), T(("□ ", C["faint"]), ("to come", C["muted"]))], w, gap=3, indent=2)
    return out


def sec_recovery(m: dict, w: int) -> list[Text]:
    rec, b, h = m["recovery"], m["baselines"], m["heart"]
    out: list[Text] = []
    if rec["score"] is None:
        out += headline(w, "none", ("—", "recovery"))
        out += takeaway("Can't score today: {}.".format(rec["reason"]), w)
    else:
        lvl = {"green": "good", "yellow": "watch", "red": "flag"}[rec["band"]]
        out += headline(w, lvl, ("{}%".format(rec["score"]), "recovery"), (rec["band"], ""))
        top = sorted(rec["contributions"], key=lambda c: abs(c["points"]), reverse=True)
        movers = [c for c in top if abs(c["points"]) >= 1][:2]
        if movers:
            out += takeaway("Driven by " + " and ".join("{} ({:+.0f} pts)".format(c["label"], c["points"])
                                                        for c in movers) + ".", w)
        out.append(blank())
        for c in rec["contributions"]:
            out.append(_diverging(c["label"], c["z"], c["points"], w))
    out.append(blank())
    days = m["days"]
    out += K.trend_chart(m["series"]["hrv"], days, w, 3, C["good"], lambda v: "{:.0f}".format(v), b.get("hrv"),
                         title="HRV · ms · higher is better")
    out.append(blank())
    out += K.trend_chart(m["series"]["rhr"], days, w, 3, C["heart"], lambda v: "{:.0f}".format(v), b.get("rhr"),
                         title="Resting HR · bpm · lower is better")
    hr = h["hr_today"]
    rest, hmax = h["hr_rest"], m.get("hr_max_est")
    if hr and rest and hmax:
        out.append(blank())
        out.append(sub("Heart rate today", w, "workout windows only"))

        def zc(v):
            f = (v - rest) / (hmax - rest)
            return C["zone"]["peak"] if f >= .85 else C["zone"]["cardio"] if f >= .6 else C["zone"]["fat_burn"] if f >= .4 else C["zone"]["light"]
        vals = [v for _, v in hr]
        lines = K.braille_line(vals, max(10, w - 5), 3, C["heart"], lo=min(rest, min(vals)), hi=max(vals),
                               zone_color=zc, xs=[t / 1440 for t, _ in hr])
        for i, ln in enumerate(lines):
            lab = "{:.0f}".format(max(vals)) if i == 0 else "{:.0f}".format(min(vals)) if i == len(lines) - 1 else ""
            out.append(fit(T(("{:>4}".format(lab), C["muted"]), ("│" if lab else " ", C["rule"]), ln), w))
        pw = max(10, w - 5)
        axis = [" "] * pw
        for frac, lab in ((0, "12a"), (0.25, "6a"), (0.5, "12p"), (0.75, "6p")):
            c = int(round(frac * (pw - 1)))
            for i, ch in enumerate(lab):
                if c + i < pw:
                    axis[c + i] = ch
        out.append(fit(T(" " * 5, ("".join(axis), C["muted"])), w))
    out.append(blank())
    zt = h["zones_today"] or {}
    rb = b.get("resp")
    lvl_txt = (h["vo2_level"] or "").replace("_", " ").lower()
    vo2 = next((v for v in reversed(m["series"]["vo2"]) if v is not None), None)
    out += details([
        ("Resp", "{:.1f}/min (avg {:.1f})".format(h["resp"], rb["mean"]) if h["resp"] is not None and rb else
         ("{:.1f}/min".format(h["resp"]) if h["resp"] is not None else "—")),
        ("SpO₂", "{:.1f}%".format(h["spo2"]) if h["spo2"] is not None else "—"),
        ("Skin temp", "{:+.1f} °C vs baseline".format(h["skin_dev"]) if h["skin_dev"] is not None else "—"),
        ("VO₂ max", "{:.1f}{}".format(vo2, " " + lvl_txt if lvl_txt else "") if vo2 else "—"),
        ("Zones today", "fat burn {} · cardio {} · peak {}".format(hm(zt.get("fat_burn") or 0), hm(zt.get("cardio") or 0),
                                                                  hm(zt.get("peak") or 0))),
        ("Stress", "— needs all-day HR" if h["stress"]["hours"] is None else
         "peak {:.1f}/3".format(max((x for x in h["stress"]["hours"] if x is not None), default=0))),
    ], w)
    return out


def _diverging(label: str, z: float | None, points: float, w: int) -> Text:
    half = max(4, min(12, (w - 30) // 2))
    f = max(-1.0, min(1.0, points / 15))
    n = int(round(abs(f) * half))
    color = C["good"] if points >= 0 else C["flag"]
    left = (" " * (half - n) + "█" * n) if points < 0 else " " * half
    right = ("█" * n + " " * (half - n)) if points > 0 else " " * half
    return fit(T(("  {:<11}".format(label), C["ink2"]), (left, color), ("│", C["rule"]), (right, color),
                 (" {:+.0f} pts".format(points), "bold " + C["ink"] if abs(points) >= 1 else C["muted"]),
                 ("  {:+.1f}σ".format(z) if z is not None else "", C["muted"])), w)


# ---------------------------------------------------------------- strain & activity

STRAIN_STATE_COLOR = {"in": "#0ca30c", "below": "#3987e5", "above": "#fab219"}


def sec_strain(m: dict, w: int) -> list[Text]:
    st, a = m["strain"], m["activity"]
    tgt = st["target"]
    out: list[Text] = []
    so_far = m["partial_day"]
    if st["day"] is None:
        lvl = "none"
    elif tgt and tgt[0] <= st["day"] <= tgt[1]:
        lvl = "good"
    elif tgt and st["day"] > tgt[1]:
        lvl = "watch"
    else:
        lvl = "none" if so_far else "watch"
    out += headline(w, lvl, ("{:.1f}".format(st["day"]) if st["day"] is not None else "—", "strain" + (" so far" if so_far else "")),
                    ("{:.0f}–{:.0f}".format(*tgt) if tgt else "—", "target"))
    bw = max(12, min(48, w - 12))
    filled = 0 if st["day"] is None else int(st["day"] / 21 * bw)
    a_, z_ = (int(tgt[0] / 21 * bw), min(bw - 1, int(tgt[1] / 21 * bw))) if tgt else (-1, -1)
    bar = Text(no_wrap=True)
    for i in range(bw):
        if i < filled:
            bar.append("█", style=K.strain_color(st["day"]))
        elif a_ <= i <= z_:
            bar.append("▒", style=C["strain"][2])
        else:
            bar.append("░", style=C["faint"])
    out.append(fit(T(("  0 ", C["muted"]), bar, (" 21", C["muted"])), w))
    out.append(fit(T(("    █ today  ▒ target range", C["muted"])), w))
    out.append(blank())
    ser = m["series"]["strain"]
    n = len(ser)
    col_w = 2 if w >= 7 + n * 3 - 1 else 1
    gap = 1 if w >= 7 + n * (col_w + 1) - 1 else 0
    if 7 + n * (col_w + gap) - gap > w:
        ser = ser[-((w - 7 + gap) // (col_w + gap)):]
    out.append(sub("Strain · {} days".format(len(ser)), w, "avg {:.1f}".format(m["baselines"]["strain"]["mean"])
                   if m["baselines"].get("strain") else ""))
    # each day against its own target (from that morning's recovery): in range, below or above
    recs = m["series"]["recovery"][-len(ser):]
    cols, tally = [], {"in": 0, "below": 0, "above": 0}
    for v, r in zip(ser, recs):
        tgt = S.strain_target(r)
        if v is None or tgt is None:
            cols.append(C["none"])
            continue
        state = "in" if tgt[0] <= v <= tgt[1] else "below" if v < tgt[0] else "above"
        tally[state] += 1
        cols.append(STRAIN_STATE_COLOR[state])
    out += [fit(x, w) for x in K.columns(ser, 4, col_w, gap, cols, top=21, label_w=6, fmt=lambda v: "{:.0f}".format(v),
                                         value_fmt=lambda v: "{:.0f}".format(v), today_fmt=lambda v: "{:.1f}".format(v))]
    def days(n: int) -> str:
        return "{} day{}".format(n, "" if n == 1 else "s")
    out += _flow([T(("█", STRAIN_STATE_COLOR["in"]), (" {} in range".format(days(tally["in"])), C["muted"])),
                  T(("█", STRAIN_STATE_COLOR["below"]), (" {} below".format(days(tally["below"])), C["muted"])),
                  T(("█", STRAIN_STATE_COLOR["above"]), (" {} above".format(days(tally["above"])), C["muted"])),
                  T(("", ""), ("each day vs its own recovery-based target", C["muted"]))], w, gap=3, indent=2)
    out.append(blank())

    goal = m["config"].get("steps_goal") or 10000
    azm_goal = m["config"].get("azm_goal") or 22
    out.append(sub("Activity" + (" so far" if so_far else ""), w, "│ marks the goal or your 28-day average"))
    bw2 = max(10, min(36, w - 34))
    for label, val, target, kind, fmt in (("Steps", a["steps"], goal, "goal", lambda v: "{:,.0f}".format(v)),
                                          ("Zone minutes", a["azm"], azm_goal, "goal", lambda v: "{:.0f}".format(v)),
                                          ("Mod+vig min", a["active_min"], a["active_min_avg"], "avg", lambda v: "{:.0f}".format(v))):
        frac = None if val is None or not target else val / (target * 1.25)
        note = "" if not target else (" / {}".format(fmt(target)) if kind == "goal" else "  avg {}".format(fmt(target)))
        out.append(K.fit_parts(w, ("  {:<13}".format(label), C["ink2"]), K.hbar(frac, bw2, C["activity"], None if not target else 0.8),
                               ("{:>8}".format(fmt(val) if val is not None else "—"), "bold " + C["ink"]),
                               (None, note, C["muted"])))
    hrs = a["hourly"]
    if hrs:
        out.append(blank())
        cell = max(1, min(3, (w - 4) // 24))
        peak = max((v for v in hrs if v), default=0)
        out.append(sub("Movement by hour", w, "minutes of light activity or more"))
        out.append(fit(T("  ", K.heat_row(hrs, cell, max(peak, 1))), w))
        axis = [" "] * (24 * cell)
        for hr_, lab in ((0, "12a"), (6, "6a"), (12, "12p"), (18, "6p")):
            for i, ch in enumerate(lab):
                if hr_ * cell + i < len(axis):
                    axis[hr_ * cell + i] = ch
        out.append(fit(T("  ", ("".join(axis), C["muted"])), w))
        out.append(fit(T(("  ░ none  ▒ some  ▓ more  █ busiest ({} min)  · not yet".format(peak), C["muted"])), w))
    out.append(blank())
    sk = a["streaks"]
    out += details([
        ("Distance", "{:.1f} km (avg {:.1f})".format(a["distance_km"], a["distance_avg"] or 0) if a["distance_km"] is not None else "—"),
        ("Floors", "{:.0f}".format(a["floors"]) if a["floors"] is not None else "—"),
        ("Calories", "{:,.0f} (basal {:,.0f})".format(a["cal_out"], a["basal_kcal"] or 0) if a["cal_out"] is not None else "—"),
        ("Sedentary", "{} (avg {})".format(hm(a["sedentary_min"]), hm(a["sedentary_avg"])) if a["sedentary_min"] is not None else "—"),
        ("10k-step streak", "{}d (best {})".format(sk["steps_goal"]["current"], sk["steps_goal"]["best"])),
        ("Workout streak", "{}d (best {})".format(sk["workout"]["current"], sk["workout"]["best"])),
    ], w)
    return out


# ---------------------------------------------------------------- workouts
#
# One row per day (what you did, how hard, for how long), one line on how the week's time split by
# intensity, then only the sessions that matter. One zone system throughout: Z1-Z5 by % of HRmax,
# with lifting as its own kind of time, because heart rate understates lifting effort.

ZONE5_COLOR = {1: "#8a8a84", 2: "#3987e5", 3: "#199e70", 4: "#eda100", 5: "#e5484d"}
ZONE5_GLYPH = {1: "░", 2: "▒", 3: "▓", 4: "█", 5: "█"}
LIFT_COLOR, LIFT_GLYPH = "#a78bfa", "▚"
IMPACT = ("RUNNING", "TRAIL_RUN", "TREADMILL_RUNNING")


def _session_mix(s: dict) -> dict:
    """Minutes by kind for one session: 'lift', or zones 1-5 (from HR samples, else its main zone)."""
    if s["type"] == "STRENGTH_TRAINING":
        return {"lift": s["minutes"]}
    zm = s.get("zone_minutes")
    if zm and sum(zm.values()) > 0:
        scale = s["minutes"] / sum(zm.values())
        return {int(z): v * scale for z, v in zm.items() if v}
    return {s.get("zone") or 1: s["minutes"]}


def _mix_bar(mix: dict, cells: int, color: bool = True) -> Text:
    """Stacked bar: lifting first, then zones 1→5, so harder time always sits at the right end."""
    order = ["lift", 1, 2, 3, 4, 5]
    parts = [(mix.get(k, 0), LIFT_GLYPH if k == "lift" else ZONE5_GLYPH[k],
              LIFT_COLOR if k == "lift" else ZONE5_COLOR[k]) for k in order]
    return K.segbar([p for p in parts if p[0] > 0], cells) if cells > 0 else Text("")


def _what(sessions: list[dict]) -> str:
    """'Push · Cardio ×2 · Trail run': lifts first, the rest in time order."""
    names: list[str] = []
    for s in sorted(sessions, key=lambda x: (x["type"] != "STRENGTH_TRAINING", x["start"])):   # lifts first
        name = s["label"]
        if s["type"] == "STRENGTH_TRAINING" and s.get("split"):
            name = split_title(s["split"])
        names.append(name)
    out, counts = [], {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    for n in dict.fromkeys(names):
        out.append(n + (" ×{}".format(counts[n]) if counts[n] > 1 else ""))
    return " · ".join(out)


def _mix_cells(mix: dict, n: int) -> list[tuple[str, str]]:
    """The textured cells of a mix bar (lifting ▚, then zones 1→5 as ░▒▓█), for drawing into a grid."""
    order = ["lift", 1, 2, 3, 4, 5]
    parts = [(mix.get(k, 0), LIFT_GLYPH if k == "lift" else ZONE5_GLYPH[k], LIFT_COLOR if k == "lift" else ZONE5_COLOR[k])
             for k in order if mix.get(k, 0) > 0]
    total = sum(p[0] for p in parts) or 1
    raw = [p[0] / total * n for p in parts]
    cells = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - cells[i], reverse=True)[:n - sum(cells)]:
        cells[i] += 1
    out = []
    for (_, g, col), k in zip(parts, cells):
        out += [(g, col)] * k
    return out


def sec_workouts(m: dict, w: int) -> list[Text]:
    """When you trained (a 7-day timeline, each session textured by intensity), time by activity,
    key sessions, records and your zones."""
    wk = m["workouts"]
    sessions = wk["week"]
    days = [d["date"] for d in wk["strip"]]
    strain_by_day = dict(zip(m["days"], m["series"]["strain"]))
    total = sum(d["minutes"] for d in wk["strip"])
    out: list[Text] = []
    hardest = max((s for s in sessions if s["strain"] is not None), key=lambda s: s["strain"], default=None)
    out += headline(w, "none", (str(len(sessions)), "sessions"), (hm(total) if total else "0m", "in 7 days"),
                    *([("{:.1f}".format(hardest["strain"]), "hardest ({} {})".format(hardest["label"], sdate(hardest["date"])[:3]))]
                      if hardest else []))
    out.append(blank())
    if not sessions:
        out.append(nodata(w, "  No workouts in the last 7 days."))
        return out

    # ---- when you trained: 6 am → midnight, one row per day, each session textured by its intensity
    out.append(section_title("When you trained", w, "6 am → midnight"))
    start, span = 6 * 60, 18 * 60
    tw = w - 14

    def X(minutes: float) -> int:
        return int(max(0, min(tw - 1, (minutes - start) / span * tw)))

    by_day: dict[str, list[dict]] = {d: [] for d in days}
    for s_ in sessions:
        by_day.setdefault(s_["date"], []).append(s_)
    week_mix: dict = {}
    for d in days:
        cells = [("·" if k % max(1, tw // 6) == 0 else " ", C["faint"]) for k in range(tw)]
        for s_ in by_day[d]:
            t0 = D.local_dt(s_["start"])
            a_ = X(t0.hour * 60 + t0.minute)
            b_ = max(a_ + 1, X(t0.hour * 60 + t0.minute + s_["minutes"]))
            mix = _session_mix(s_)
            for k, v in mix.items():
                week_mix[k] = week_mix.get(k, 0) + v
            for i, cell in enumerate(_mix_cells(mix, b_ - a_)):
                if a_ + i < tw:
                    cells[a_ + i] = cell
        is_today = d == m["date"]
        row = T(("  {:<4}".format(sdate(d)[:3]), ("bold " + C["ink"]) if is_today else C["muted"]))
        for g, col in cells:
            row.append(g, style=col)
        st = strain_by_day.get(d)
        if by_day[d]:
            row.append("  {:>5}".format("{:.1f}".format(st) if st is not None else "—"), style="bold " + K.strain_color(st))
        else:
            row.append("  {:>5}".format("so far" if is_today and m["partial_day"] else "rest"), style=C["muted"])
        out.append(fit(row, w))
    ticks, pos = Text("      ", no_wrap=True), 0
    for hh, lab in ((6, "6a"), (9, "9a"), (12, "12p"), (15, "3p"), (18, "6p"), (21, "9p")):
        x = X(hh * 60)
        ticks.append(" " * max(0, x - pos) + lab, style=C["muted"])
        pos = x + len(lab)
    out.append(fit(ticks, w))
    tot = sum(week_mix.values()) or 1
    easy = week_mix.get(1, 0) + week_mix.get(2, 0)
    hard = week_mix.get(4, 0) + week_mix.get(5, 0)
    out += _flow([T((ZONE5_GLYPH[2], ZONE5_COLOR[2]), (" easy Z1–2 ", C["ink2"]), ("{:.0f}%".format(100 * easy / tot), "bold " + C["ink"])),
                  T((ZONE5_GLYPH[3], ZONE5_COLOR[3]), (" moderate Z3 ", C["ink2"]), ("{:.0f}%".format(100 * week_mix.get(3, 0) / tot), "bold " + C["ink"])),
                  T((ZONE5_GLYPH[4], ZONE5_COLOR[4]), (" hard Z4–5 ", C["ink2"]), ("{:.0f}%".format(100 * hard / tot), "bold " + C["ink"])),
                  T((LIFT_GLYPH, LIFT_COLOR), (" lifting ", C["ink2"]), (hm(week_mix.get("lift", 0)) or "0m", "bold " + C["ink"]))], w, 3, 2)
    out.append(blank())

    # ---- time by activity, each bar split by intensity the same way
    out.append(section_title("Time by activity", w))
    by_type: dict[str, dict] = {}
    for s_ in sessions:
        e = by_type.setdefault(s_["type"], {"label": s_["label"], "n": 0, "min": 0.0, "mix": {}})
        e["n"] += 1
        e["min"] += s_["minutes"]
        for k, v in _session_mix(s_).items():
            e["mix"][k] = e["mix"].get(k, 0) + v
    top = max(e["min"] for e in by_type.values()) or 1
    bar_w = max(10, w - 32)
    for t_, e in sorted(by_type.items(), key=lambda kv: -kv[1]["min"]):
        cells = max(1, int(round(e["min"] / top * bar_w)))
        bar = _mix_bar(e["mix"], cells)
        out.append(fit(T(("  ", ""), K.chip(t_), (" {:<10}".format(e["label"][:10]), C["ink2"]), bar, " " * (bar_w - bar.cell_len),
                         ("  {:>5}".format(hm(e["min"])), "bold " + C["ink"]), ("  ×{}".format(e["n"]), C["muted"])), w))
    out.append(blank())

    # ---- the sessions that matter: lifts first (muscle is the goal), then the hardest and longest cardio
    lifts = [s_ for s_ in sessions if s_["type"] == "STRENGTH_TRAINING"]
    cardio = sorted((s_ for s_ in sessions if s_["type"] != "STRENGTH_TRAINING" and s_["strain"] is not None),
                    key=lambda s_: s_["strain"], reverse=True)
    runs = sorted((s_ for s_ in sessions if s_["type"] in IMPACT and s_["distance_km"]), key=lambda s_: s_["distance_km"], reverse=True)
    key: list[dict] = []
    for s_ in lifts + cardio[:1] + runs[:1]:
        if s_ not in key:
            key.append(s_)
    key.sort(key=lambda s_: (s_["date"], s_["start"]), reverse=True)
    out.append(section_title("Key sessions", w))
    for s_ in key[:4]:
        if s_["type"] == "STRENGTH_TRAINING":
            name = split_title(s_.get("split")) if s_.get("split") else "Lift"
            name += "~" if s_.get("split_source") == "assumed" else ""
            detail = " · ".join(s_.get("muscles") or []) or "muscles not tagged"
        else:
            name = s_["label"]
            bits = []
            if s_["distance_km"]:
                bits.append("{:.1f} km".format(s_["distance_km"]))
            if s_["pace_s_per_km"] and s_["type"] in IMPACT:
                bits.append("{}:{:02d}/km".format(*divmod(int(s_["pace_s_per_km"]), 60)))
            if s_.get("zone"):
                bits.append("mostly Z{}".format(s_["zone"]))
            detail = " · ".join(bits)
        st = s_["strain"]
        head = T(("  {:<4}".format(sdate(s_["date"])[:3]), C["ink2"]), K.chip(s_["type"]), (" {:<10}".format(name[:10]), "bold " + C["ink"]),
                 ("{:>6}  ".format(hm(s_["minutes"])), C["ink2"]))
        tail = T(("{:>5}".format("{:.1f}".format(st) if st is not None else "—"), "bold " + K.strain_color(st)))
        room = w - head.cell_len - tail.cell_len - 1
        out.append(fit(head + T((detail[:room].ljust(room), C["muted"])) + Text(" ") + tail, w))
    if len(sessions) > len(key[:4]):
        out.append(fit(T(("  + {} other sessions this week".format(len(sessions) - len(key[:4])), C["muted"])), w))
    out.append(blank())
    pr = wk["records"]
    recs = []
    if pr["longest_run"]:
        recs.append("longest run {:.1f} km".format(pr["longest_run"]["km"]))
    if pr["fastest_pace"]:
        recs.append("fastest {}:{:02d}/km".format(*divmod(int(pr["fastest_pace"]["s_per_km"]), 60)))
    if pr["highest_strain"]:
        recs.append("top strain {:.1f}".format(pr["highest_strain"]["strain"]))
    out += details([("Records", " · ".join(recs))], w)
    zb = m.get("hr_zones_bpm")
    if zb:
        out += details([("Zones", "Z1 <{} · Z2 {}–{} · Z3 {}–{} · Z4 {}–{} · Z5 {}+ bpm".format(
            zb[1], zb[1], zb[2], zb[2], zb[3], zb[3], zb[4], zb[4]))], w)
    return out


# ---------------------------------------------------------------- muscle freshness (card grid)

MUSCLE_NAMES = {"chest": "Chest", "shoulders": "Shoulders", "lats": "Lats/Back", "biceps": "Biceps",
                "triceps": "Triceps", "abs": "Abs", "quads": "Quads", "hamstrings": "Hamstrings",
                "glutes": "Glutes", "calves": "Calves"}
MF = {"bg": "#1c1c1e", "border": "#3a3a3c", "title": "#ffffff", "dim": "#8e8e93", "off": "#3a3a3c"}
CARD_TEXT_W = 14          # "100% recovered"
CARD_W = MI.W + 2 + CARD_TEXT_W
FRESH_MAX_W = 76          # standalone panel: two card columns, not stretched across a wide terminal
FRESH_TWO_COL = 70        # under this panel width, one card per row


def split_title(day_name: str | None) -> str:
    return "—" if not day_name else day_name.split(" (")[0].title()


def _tint(hexcolor: str, amount: float = 0.45) -> str:
    """Mix a color toward white: the "glow" on the last filled dot."""
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    mix = lambda c: int(round(c + (255 - c) * amount))  # noqa: E731
    return "#{:02x}{:02x}{:02x}".format(mix(r), mix(g), mix(b))


def fresh_dots(pct: int, color: bool = True) -> Text:
    """10 dots: one per 10 %, rounded down. Filled dots in the state color with the last one glowing;
    unfilled ones dark gray (or ○ without color)."""
    k = S.freshness_dots(pct)
    col = S.freshness_color(pct)
    t = Text(no_wrap=True)
    for i in range(10):
        if i < k:
            t.append("●", style=("bold " + _tint(col)) if i == k - 1 else col)
        else:
            t.append("●" if color else "○", style=MF["off"])
    return t


CARD_ROWS = MI.H // 2      # card height = icon height
CARD_TEXT_AT = {1: "name", 2: "pct", 4: "dots"}   # like the reference: name and % at the top, dots at the bottom


def _card(row: dict, color: bool, now: datetime | None = None) -> list[Text]:
    """Icon, name, % in its freshness color, when it's ready again, and the dots."""
    pct = row["fresh"]
    icon = MI.render(row["muscle"], S.freshness_color(pct), color=color)
    h = row.get("ready_in_h") or 0
    if h <= 0:
        when = T(("ready now", C["good"]))
    else:
        t = (now or datetime.now(D.NY)) + timedelta(hours=h)
        when = T(("ready " + t.strftime("%a %-I%p").replace("AM", "a").replace("PM", "p"), MF["dim"]))
    parts = {1: T((MUSCLE_NAMES[row["muscle"]], "bold " + MF["title"])),
             2: T(("{}%".format(pct), "bold " + S.freshness_color(pct))), 3: when, 4: fresh_dots(pct, color)}
    out = []
    for i in range(CARD_ROWS):
        text = parts.get(i, Text(""))
        out.append(icon[i] + Text("  ") + text + Text(" " * max(0, CARD_TEXT_W - text.cell_len)))
    return out


def sec_freshness(m: dict, w: int, color: bool = True, sort: str = "default") -> list[Text]:
    """The Muscle Freshness card grid, most recovered first (`--sort freshness`: least recovered first),
    each card with when that muscle is back to 90 %."""
    mu = m["muscles"]
    out: list[Text] = []
    order_note = "least recovered first" if sort == "freshness" else "most recovered first"
    head = T(("Muscle Freshness", "bold " + MF["title"]), ("  " + order_note, MF["dim"]))
    out.append(fit(head, w))
    if not mu.get("has_strength"):
        out.append(fit(T(("· no strength sessions logged", MF["dim"])), w))
        out += K.para("Log sets with: fitdash lift bench 3x8@60 row 4x10@50 (or tag a session: "
                      "fitdash tag <date> <split day>)", w, MF["dim"])
        return out
    rows = list(mu["rows"])
    ready = sum(1 for r in rows if r["fresh"] >= 90)
    nxt = (m["training"]["split"] or {}).get("next")
    line = T(("{}/{}".format(ready, len(rows)), "bold " + MF["title"]), (" muscles ready now", MF["dim"]))
    if nxt:
        line.append(" · next to train: ", style=MF["dim"])
        line.append(nxt, style="bold " + MF["title"])
    out += K.para(line.plain, w, MF["dim"]) if line.cell_len > w else [fit(line, w)]
    out.append(blank())
    key = (lambda r: (r["fresh"], S.MUSCLES.index(r["muscle"]))) if sort == "freshness" else \
          (lambda r: (-r["fresh"], S.MUSCLES.index(r["muscle"])))
    now = datetime.fromisoformat(m["now"]) if m.get("now") else None
    cards = [_card(r, color, now) for r in sorted(rows, key=key)]
    cols = 2 if w + 6 >= FRESH_TWO_COL else 1                 # panel width = w + borders + padding
    gutter = max(4, (w - cols * CARD_W) // cols) if cols == 2 else 0
    for i in range(0, len(cards), cols):
        group = cards[i:i + cols]
        for r in range(CARD_ROWS):
            ln = Text(no_wrap=True)
            for j, c in enumerate(group):
                if j:
                    ln.append(" " * gutter)
                ln.append_text(c[r])
            out.append(fit(ln, w))
        if i + cols < len(cards):
            out.append(blank())
    out.append(blank())
    assumed = [x for x in mu["sessions"] if x["source"] == "assumed"]
    from_sets = [x for x in mu["sessions"] if x["source"] == "sets"]
    note = "ready = back to 90 %, if you don't train it again"
    if from_sets:
        note += " · from your logged sets: " + ", ".join(sdate(x["date"])[:3] for x in from_sets)
    if assumed:
        note += " · assumed from your split: " + ", ".join(
            "{} {}".format(sdate(x["date"])[:3], split_title(x["what"]).lower()) for x in assumed)
    if mu["unlabeled_recent"]:
        note += " · untagged lift {}: fitdash tag".format(", ".join(sdate(x)[:3] for x in mu["unlabeled_recent"]))
    out += K.para(note, w, MF["dim"])
    return out


def freshness_panel(m: dict, width: int, color: bool, sort: str = "default") -> Panel:
    """Rounded card on a dark background, title inside like the reference."""
    return Panel(Group(*sec_freshness(m, width - 6, color, sort)), box=box.ROUNDED,
                 border_style=MF["border"], padding=(0, 2), width=width,
                 style=("on " + MF["bg"]) if color else "")


# ---------------------------------------------------------------- training plan

def tex_bar(frac: float | None, width: int, color: str, marker: float | None = None, track: str = "·") -> Text:
    """A textured bar: ▓ cells (a gritty, printed look) instead of solid blocks. `marker` (0-1)
    draws a │ for a limit or goal."""
    t = Text(no_wrap=True)
    n = 0 if frac is None else int(round(max(0.0, min(1.0, frac)) * width))
    mk = None if marker is None else min(width - 1, int(round(max(0.0, min(1.0, marker)) * width)))
    for i in range(width):
        if i == mk:
            t.append("│", style=C["ink2"])
        elif i < n:
            t.append("▓", style=color)
        else:
            t.append(track, style=C["faint"])
    return t


LOAD_ZONES = ((0.0, 0.8, "too little", "#3987e5"), (0.8, 1.3, "sweet spot", "#0ca30c"),
              (1.3, 1.5, "caution", "#fab219"), (1.5, 2.0, "risky", "#d03b3b"))


def load_gauge(ratio: float | None, w: int) -> list[Text]:
    """The 7 ÷ 28-day load ratio on a 0–2 scale, in one row: every zone is a thin centered line in its
    color except the one you're in, which is a thick textured block, so where you are stands out."""
    gw = max(20, w - 8)                               # "  0 " + gauge + " 2"
    active = None if ratio is None else next((z for z in LOAD_ZONES if z[0] <= min(ratio, 1.999) < z[1]), LOAD_ZONES[-1])
    bar = T(("  0 ", C["muted"]))
    for z in LOAD_ZONES:
        a, b, _, col = z
        cells = round(b / 2 * gw) - round(a / 2 * gw)
        bar.append(("▓" if z is active else "─") * cells, style=col)
    bar.append(" 2", style=C["muted"])
    out = [fit(bar, w)]
    if ratio is not None:
        x = min(gw - 1, round(min(2.0, ratio) / 2 * gw))
        label = "▲ {:.2f} {}".format(ratio, active[2])
        x = max(0, min(x, gw - len(label)))
        out.append(fit(T(("    " + " " * x, ""), (label, "bold " + C["ink"])), w))
    names, pos = Text("    ", no_wrap=True), 0
    for z in LOAD_ZONES:
        a, b, name, col = z
        x = round((a + b) / 4 * gw) - len(name) // 2
        names.append(" " * max(1 if pos else 0, x - pos) + name, style=("bold " + col) if z is active else col)
        pos = max(pos, x) + len(name)
    out.append(fit(names, w))
    return out


def sec_training(m: dict, w: int) -> list[Text]:
    """Split queue as readiness bars, the training-load gauge, and running/impact minutes per week
    against your limit."""
    tr = m["training"]
    sp = tr.get("split") or {}
    out: list[Text] = []
    out += headline(w, "good" if tr["gym_week"] >= tr["gym_goal"] - 1 else "watch",
                    ("{}/{}".format(tr["gym_week"], tr["gym_goal"]), "gym sessions this week"),
                    ("next: " + (sp.get("next") or "—"), ""))
    if not sp.get("next") and sp.get("reason"):
        out += takeaway(sp["reason"], w)
    out.append(blank())

    queue = sp.get("queue") or []
    if queue:
        out.append(section_title("Split queue", w, "ready = least-recovered muscle in that day"))
        name_w = min(22, max(len(d_) for d_ in queue) + 2)
        bar_w = max(8, w - name_w - 22)
        for i, d_ in enumerate(queue):
            fresh = (sp.get("fresh") or {}).get(d_)
            col = S.freshness_color(fresh)
            out.append(fit(T(("  {:<7}".format("▶ next" if i == 0 else "{}.".format(i + 1)), "bold " + C["strain"][4] if i == 0 else C["muted"]),
                             ("{:<{}}".format(_short(d_, name_w - 1), name_w), "bold " + C["ink"] if i == 0 else C["ink2"]),
                             tex_bar((fresh or 0) / 100, bar_w, col), ("  {:>3}% ready".format(fresh if fresh is not None else "—"), col)), w))
        if sp.get("last_done"):
            out.append(fit(T(("  last: {} on {}".format(sp["last_done"], sdate(sp["last_done_date"])), C["muted"])), w))
        out.append(blank())

    ac = tr.get("acwr") or {}
    out.append(section_title("Training load", w, "last 7 days ÷ last 28 days"))
    if ac.get("ratio") is None:
        out += nodata_lines(w, ac.get("reason") or "needs a few weeks of activity data")
    else:
        out += load_gauge(ac["ratio"], w)
    out.append(blank())

    weeks = tr.get("impact_weeks") or []
    if weeks:
        lim = tr.get("impact_limit")
        out.append(section_title("Running & impact", w, "minutes per week" + (" · │ limit {:.0f}".format(lim) if lim else "")))
        mx = max([x["minutes"] or 0 for x in weeks] + [lim or 0]) or 1
        bw = max(10, w - 26)
        for x in weeks:
            v = x["minutes"]
            col = C["watch"] if x.get("spike") else C["none"] if x.get("partial") else C["strain"][4]
            out.append(fit(T(("  {:<7}".format("now" if x.get("partial") else sdate(x["start"])[4:]), C["muted"]),
                             tex_bar((v or 0) / mx, bw, col, marker=(lim / mx) if lim else None, track=" "),
                             ("  {:>4}".format(v if v is not None else "—"), "bold " + C["ink"]),
                             ("  spike" if x.get("spike") else "", C["watch"])), w))
        if tr.get("impact_spike_recent"):
            out += takeaway("Over 30% above your 4-week average: no running or jumping until the knee is checked"
                            + (", then keep this week under {:.0f} min.".format(lim) if lim else "."), w)
    return out


def _split_track(sp: dict, w: int) -> list[Text]:
    """The split as a row of chips: last lift (✓ with its day) → NEXT (heavy border) → the rest."""
    order = [sp["last_done"]] + sp["queue"][:-1]
    arrow = " ──▶ "
    chip_w = min(14, (w - 2 - len(arrow) * (len(order) - 1)) // len(order))
    if chip_w < 8:
        return takeaway("Then " + " → ".join(sp["queue"][1:]) + ".", w)
    inner = chip_w - 2
    rows = [Text("  ", no_wrap=True) for _ in range(5)]
    for i, name in enumerate(order):
        main, _, muscles = name.partition(" (")
        muscles = muscles.rstrip(")")
        if i == 0:
            status, st_style, edge, border = "✓ " + date.fromisoformat(sp["last_done_date"]).strftime("%a %-m/%-d"), C["good"], "╭╮│╰╯─", C["muted"]
        else:
            f = (sp.get("fresh") or {}).get(name)
            pct = "" if f is None else "{}%".format(f)
            if i == 1:
                status, st_style, edge, border = ("▶ NEXT " + pct).strip(), "bold " + C["activity"], "┏┓┃┗┛━", C["activity"]
            else:
                status, st_style, edge, border = (pct + " fresh") if pct else "", C["muted"], "╭╮│╰╯─", C["rule"]
        tl, tr, v, bl, br, h = edge
        texts = [(main.upper()[:inner].center(inner), "bold " + (C["ink"] if i == 1 else C["ink2"])),
                 (muscles[:inner].center(inner), C["muted"]),
                 (status[:inner].center(inner), st_style)]
        if i:
            for r in range(5):
                rows[r].append(arrow if r == 2 else " " * len(arrow), style=C["muted"])
        rows[0].append(tl + h * inner + tr, style=border)
        for r, (txt, style) in enumerate(texts, start=1):
            rows[r].append(v, style=border)
            rows[r].append(txt, style=style)
            rows[r].append(v, style=border)
        rows[4].append(bl + h * inner + br, style=border)
    return [fit(r, w) for r in rows]


def _weight_corridor(b: dict, today: date, w: int, h: int = 7) -> list[Text]:
    """Weigh-ins (●) against the lean-bulk corridor: +0.25–0.5 %/week compounding from the first
    weigh-in, drawn out to today so the gap since the last weigh-in is visible."""
    pts = [(date.fromisoformat(d), kg) for d, kg in b["weights"]]
    d0, kg0 = pts[0]
    end = max(today, pts[-1][0])
    span_days = max(1, (end - d0).days)
    lab_w = 6
    plot_w = max(16, w - lab_w - 1)

    def band(day_offset: float) -> tuple[float, float]:
        weeks = day_offset / 7
        return kg0 * (1 + S.BULK_PACE[0] / 100) ** weeks, kg0 * (1 + S.BULK_PACE[1] / 100) ** weeks

    lo_end, hi_end = band(span_days)
    lo = min([kg for _, kg in pts] + [kg0]) - 0.3
    hi = max([kg for _, kg in pts] + [hi_end]) + 0.3

    def row(v: float) -> int:
        return int(round((hi - v) / (hi - lo) * (h - 1)))

    grid = [[(" ", "")] * plot_w for _ in range(h)]
    for c in range(plot_w):
        blo, bhi = band(c / (plot_w - 1) * span_days)
        for r in range(row(bhi), row(blo) + 1):
            grid[r][c] = ("░", C["body"])
    for d, kg in pts:
        c = int(round((d - d0).days / span_days * (plot_w - 1)))
        r = row(kg)
        grid[r][c] = ("●", "bold " + C["ink"])
        lab = " {:.1f}".format(kg)
        start = c + 1 if c + 1 + len(lab) <= plot_w else c - len(lab)
        for j, ch in enumerate(lab):
            if 0 <= start + j < plot_w and grid[r][start + j][0] in (" ", "░"):
                grid[r][start + j] = (ch, "bold " + C["ink"])
    out = []
    for r in range(h):
        v = hi - r / (h - 1) * (hi - lo)
        lab = "{:.1f}".format(v) if r in (0, h - 1) else ""
        t = T(("{:>{w}} ".format(lab, w=lab_w - 1), C["muted"]), ("┤" if lab else "│", C["rule"]))
        for ch, st in grid[r]:
            t.append(ch, style=st)
        out.append(fit(t, w))
    first, last = d0.strftime("%b %-d"), "today" if end == today else end.strftime("%b %-d")
    out.append(fit(T(" " * (lab_w + 1), (first, C["muted"]),
                     " " * max(1, plot_w - len(first) - len(last)), (last, "bold " + C["ink2"])), w))
    out += _flow([T(("░", C["body"]), (" lean-bulk corridor (+0.25–0.5 %/wk from {:.1f} kg)".format(kg0), C["muted"])),
                  T(("● weigh-in", C["muted"]))], w, 3, 2)
    out += takeaway("Target by today: {:.1f}–{:.1f} kg. Last weigh-in {:.1f} kg on {}.".format(
        lo_end, hi_end, pts[-1][1], pts[-1][0].strftime("%b %-d")), w)
    return out


# ---------------------------------------------------------------- body & nutrition

def sec_body(m: dict, w: int) -> list[Text]:
    """Weight trend as a lean-bulk pace gauge, the weigh-ins against the lean-bulk corridor, and a
    few quiet details."""
    b = m["body"]
    out: list[Text] = []
    if not b.get("latest_kg"):
        out += headline(w, "none", ("No weigh-ins", "yet"))
        out += takeaway("Weigh in on your Fitbit scale or log weight in the Fitbit app; the lean-bulk pace "
                        "appears after a few weigh-ins.", w)
        return out
    tr = b["trend"]
    rate = tr.get("kg_per_week")
    on_pace = tr.get("status") in ("on pace", "at the low edge", "at the high edge")
    out += headline(w, "good" if on_pace else "watch" if rate is not None else "none",
                    ("{:.1f} kg".format(b["latest_kg"]), "latest"),
                    ("{:+.2f} kg/wk".format(rate) if rate is not None else "—", tr.get("status") or tr.get("reason") or ""))
    out.append(blank())

    # pace gauge: −0.2 … +0.8 kg/week, the lean-bulk range in green, your trend as ●
    out.append(section_title("Lean-bulk pace", w, "kg per week"))
    lo, hi = b.get("pace_target_kg") or (0.16, 0.32)
    gw = max(20, w - 6)

    def X(v: float) -> int:
        return int(round((max(-0.2, min(0.8, v)) + 0.2) / 1.0 * (gw - 1)))

    row = [("─", C["faint"])] * gw
    for i in range(X(lo), X(hi) + 1):
        row[i] = ("━", C["good"])
    row[X(0)] = ("│", C["rule"])
    if rate is not None:
        row[X(rate)] = ("●", "bold " + C["ink"])
    t = Text("  ", no_wrap=True)
    for ch, col in row:
        t.append(ch, style=col)
    out.append(fit(t, w))
    labels, pos = Text("  ", no_wrap=True), 0
    for v, lab in ((-0.2, "−0.2"), (0, "0"), (lo, "{:.2f}".format(lo)), (hi, "{:.2f}".format(hi)), (0.8, "+0.8")):
        x = X(v) - (len(lab) - 1 if v == 0.8 else 0)
        gap = max(1 if pos else 0, x - pos)
        labels.append(" " * gap + lab, style=C["good"] if v in (lo, hi) else C["muted"])
        pos += gap + len(lab)
    out.append(fit(labels, w))
    out += takeaway("━ is a lean bulk (0.25–0.5 % of body weight a week). ● is your trend from {} weigh-in{}."
                    .format(len(b["weights"]), "" if len(b["weights"]) == 1 else "s"), w)
    out.append(blank())
    out += _weight_corridor(b, date.fromisoformat(m["date"]), w)
    out.append(blank())
    stale = b.get("days_since_weigh_in")
    out += details([("Weigh-in", "today" if stale == 0 else "{} days ago".format(stale) if stale is not None else "—"),
                    ("Food", "not logged" if b.get("cal_in_today") is None else "{:,.0f} kcal".format(b["cal_in_today"])),
                    ("BMI", "{}".format(b.get("bmi") or "—"))], w)
    return out


def sec_logs(m: dict, w: int) -> list[Text]:
    out = []
    for k, status in m["logs"].items():
        label = {"scope_not_granted": "permission not granted", "denied": "permission denied", "ok": "no entries",
                 "empty": "no entries", "not requested": "not available from this API"}.get(status, status)
        out.append(nodata(w, "{:<12} {}".format(k.title(), label)))
    return out


# ---------------------------------------------------------------- period view

def sec_period(m: dict, w: int) -> list[Text]:
    p = m["period"]
    out = [fit(T(("{} days: {} → {}".format(len(p["days"]), sdate(p["days"][0]), sdate(p["days"][-1])), "bold " + C["ink"]),
                 ("   vs {} → {}".format(sdate(p["prev_days"][0]), sdate(p["prev_days"][1])), C["muted"])), w)]
    if p["excluded_today"]:
        out += nodata_lines(w, "today is in progress, so it's left out of totals like steps and strain", 0)
    out.append(blank())
    spark_w = min(len(p["days"]), max(7, w - 64)) if w >= 100 else min(len(p["days"]), max(7, w - 46))
    show_best = w >= 100
    hdr = T(("{:<13}{:>9}{:>11} ".format("", "avg", "vs prev"), C["muted"]), ("{:<{n}}".format("trend", n=spark_w), C["muted"]))
    if show_best:
        hdr.append("  best           worst", style=C["muted"])
    out.append(fit(hdr, w))
    for r in p["rows"]:
        name = r["metric"]
        fmt = (lambda v: "{:,.0f}".format(v)) if abs(r["avg"] or 0) >= 100 else (lambda v: "{:.1f}".format(v))
        if r["avg"] is None:
            out.append(nodata(w, "{:<13}{:>9}".format(name, "—")))
            continue
        delta = None if r["prev"] is None else r["avg"] - r["prev"]
        hb = r.get("higher_better")
        if delta is None or hb is None or abs(delta) < 1e-9:
            dstyle = C["ink2"]
        else:
            dstyle = C["good"] if (delta > 0) == hb else C["flag"]
        arrow = "" if delta is None else ("▲" if delta > 0 else "▼" if delta < 0 else "=")
        verdict = "" if delta is None or hb is None or abs(delta) < 1e-9 else (" ✓" if (delta > 0) == hb else " !")
        line = T(("{:<13}".format(name), C["ink2"]), ("{:>9}".format(fmt(r["avg"])), "bold " + C["ink"]),
                 ("{:>11} ".format("—" if delta is None else "{}{}{}".format(arrow, fmt(abs(delta)), verdict)), dstyle),
                 K.spark(r["series"][-spark_w:], C["strain"][4]))
        if show_best and r.get("best"):
            line.append("  {:<15}{}".format("{} {}".format(sdate(r["best"][0])[4:], fmt(r["best"][1])),
                                             "{} {}".format(sdate(r["worst"][0])[4:], fmt(r["worst"][1]))), style=C["muted"])
        out.append(fit(line, w))
    return out


# ---------------------------------------------------------------- this week vs last

WEEK_ROWS = [  # (metric, label, value format, unit for the change, weekly total?, smallest change that counts)
    ("Recovery %", "Recovery", lambda v: "{:.0f}%".format(v), "pts", False, 2),
    ("Sleep score", "Sleep score", lambda v: "{:.0f}".format(v), "pts", False, 3),
    ("Asleep h", "Sleep", lambda v: hm(v * 60), "h", False, 10 / 60),
    ("HRV ms", "HRV", lambda v: "{:.0f} ms".format(v), "ms", False, 2),
    ("Resting HR", "Resting HR", lambda v: "{:.0f} bpm".format(v), "bpm", False, 1),
    ("Strain", "Strain", lambda v: "{:.1f}".format(v), "", False, 0.5),
    ("Steps", "Steps/day", lambda v: "{:,.0f}".format(v), "", False, 500),
    ("Workout min", "Training", lambda v: hm(v), "min", True, 15),
    ("Lifting min", "Lifting", lambda v: hm(v), "min", True, 10),
    ("Weight kg", "Weight", lambda v: "{:.1f} kg".format(v), "kg", False, 0.1),
]


WEEK_SPAN = 50.0          # the change bars run from −50 % to +50 %; bigger changes hit the edge (▸/◂)


def _delta_text(key: str, delta: float, total: bool, unit: str) -> str:
    if delta == 0:
        return "same"
    sign = "+" if delta > 0 else "−"
    if total:
        return sign + hm(abs(delta))
    if key == "Asleep h":
        return sign + hm(abs(delta) * 60)
    if key == "Steps":
        return "{}{:,.0f}".format(sign, abs(delta))
    return "{}{:.1f}{}".format(sign, abs(delta), (" " + unit) if unit and unit != "pts" else "")


def sec_week(m: dict, w: int) -> list[Text]:
    """The last 7 days vs the 7 before. Each row: last week's value, a textured bar of the % change
    growing left or right from a shared center line, this week's value, and the change. Green is
    better for you, amber worse, gray within day-to-day noise. Today counts once it's over."""
    p = m.get("week_review")
    if not p:
        return [nodata(w, "  Not enough history for a week-over-week comparison.")]
    rows = {r["metric"]: r for r in p["rows"]}
    label_w, val_w, chg_w = (11, 9, 11) if w >= 70 else (11, 7, 9)
    fixed = 2 + label_w + val_w + 2 + 2 + val_w + chg_w + 2       # everything but the bar
    half = max(4, min(18, (w - fixed - 1) // 2))
    plot_w = 2 * half + 1
    better = worse = 0
    lines = []
    for key, label, fmt, unit, total, noise in WEEK_ROWS:
        r = rows.get(key)
        if not r or r["avg"] is None:
            continue
        cur = r["avg"] * (r["n"] if total else 1)
        prev = None if r["prev"] is None else r["prev"] * (r["n"] if total else 1)
        hb = r.get("higher_better")
        delta = None if prev is None else cur - prev
        if delta is not None and abs(delta) < noise:
            delta = 0.0
        pct = None if delta is None or not prev else 100 * delta / abs(prev)
        good = None if delta in (None, 0.0) or hb is None else (delta > 0) == hb
        col = C["rule"] if good is None else C["good"] if good else C["watch"]
        if good is not None:
            better += good
            worse += not good
        bar = [(" ", "")] * plot_w
        bar[half] = ("│", C["rule"])
        if pct:
            n = max(1, int(round(min(abs(pct), WEEK_SPAN) / WEEK_SPAN * half)))
            for k in range(1, n + 1):
                bar[half + k if pct > 0 else half - k] = ("▓", col)
            if abs(pct) > WEEK_SPAN:
                bar[plot_w - 1 if pct > 0 else 0] = ("▸" if pct > 0 else "◂", col)
        t = T(("  {:<{}}".format(label, label_w), C["ink2"]),
              ("{:>{}}".format(fmt(prev) if prev is not None else "—", val_w), C["muted"]), "  ")
        for g, c in bar:
            t.append(g, style=c)
        t.append("  {:<{}}".format(fmt(cur), val_w), style="bold " + C["ink"])
        dtxt = "—" if delta is None else _delta_text(key, delta, total, unit)
        t.append("{:>{}}".format(dtxt, chg_w), style=C["ink2"] if good is None else ("bold " + col))
        t.append(" " + ("✓" if good else "!" if good is False else " "), style="bold " + col)
        lines.append(fit(t, w))

    out = headline(w, "good" if better > worse else "watch" if worse > better else "none",
                   ("{} better".format(better), ""), ("{} worse".format(worse), "than the week before"))
    out.append(fit(T(("  {} → {}  vs  {} → {}".format(sdate(p["days"][0]), sdate(p["days"][-1]),
                                                    sdate(p["prev_days"][0]), sdate(p["prev_days"][1])), C["muted"])), w))
    out.append(blank())
    axis = [" "] * plot_w
    for pos, lab in ((0, "−{:.0f}%".format(WEEK_SPAN)), (half, "0"), (plot_w - 1, "+{:.0f}%".format(WEEK_SPAN))):
        i = 0 if pos == 0 else plot_w - len(lab) if pos == plot_w - 1 else pos
        axis[i:i + len(lab)] = list(lab)
    last_h, this_h = ("last week", "this week") if val_w >= 9 else ("last", "this")
    out.append(fit(T(("  {:<{}}{:>{}}  ".format("", label_w, last_h, val_w), C["muted"]), ("".join(axis), C["muted"]),
                     ("  {:<{}}{:>{}}".format(this_h, val_w, "change", chg_w), C["muted"])), w))
    out += lines
    out.append(blank())
    out += _flow([T(("▓", C["good"]), (" better", C["muted"])), T(("▓", C["watch"]), (" worse", C["muted"])),
                  T(("▓", C["rule"]), (" about the same", C["muted"])), T(("", ""), ("bar = % change from last week", C["muted"]))],
                 w, gap=3, indent=2)
    out += K.para("Averages per day; Training and Lifting are totals over the same number of days. Today counts once "
                  "it's over. Changes smaller than normal day-to-day noise count as the same.", w, C["muted"], indent=2,
                  prefix=Text("  "))
    return out


# ---------------------------------------------------------------- footer

def sec_footer(m: dict, w: int) -> list[Text]:
    st = m.get("category_status") or []
    have = set(m.get("metrics") or [])
    missing = sorted({r["category"].replace("_", " ") for r in st if r["status"] in ("scope_not_granted", "denied")})
    empty = sorted({r["grp"] for r in st if r["status"] == "empty" and r["grp"].replace("-", "_") not in have})
    today = date.fromisoformat(m["date"])
    stale = ["{} (last {})".format(cat, sdate(c["last"])) for cat, c in (m.get("coverage") or {}).items()
             if cat in ("activity", "heart", "sleep", "spo2_breathing_temperature") and c.get("last")
             and (today - date.fromisoformat(c["last"])).days > 1]
    bw = m.get("baseline_window") or ["?", "?"]
    pairs = [("Source", "Google Health API · " + (", ".join(m.get("devices") or []) or "unknown device")),
             ("Baseline", "{} → {}".format(sdate(bw[0]), sdate(bw[1]))),
             ("HRmax", "{:.0f} bpm (99.5th pct)".format(m["hr_max_est"]) if m.get("hr_max_est") else ""),
             ("Stale", ", ".join(stale)), ("Not granted", ", ".join(missing)), ("No data", ", ".join(empty))]
    out = details(pairs, w)
    out += K.para("{} · estimated, not WHOOP's proprietary algorithm · missing days are gaps, not zeros · "
                  "not medical advice".format(m["formula_version"]), w, C["muted"])
    return out


# ---------------------------------------------------------------- focus (default view)



def _rings_only(m: dict, w: int) -> list[Text]:
    """The three rings with labels, centered, without the verdict column."""
    inner = min(w, 119)
    rows = sec_overview(dict(m, verdict={"headline": "", "level": "none", "reasons": []}), inner, header=False)
    pad = Text(" " * ((w - inner) // 2))          # keep the rings centered in a wide Today box
    return [fit(pad + r, w) for r in rows[:11]]   # 9 ring rows + 2 label rows


def _attention(m: dict) -> list[tuple[str, str]]:
    """Only what needs action today, most important first (computed in data.attention)."""
    return [tuple(x) for x in m.get("attention") or []]


def _priority_lines(m: dict, w: int) -> list[Text]:
    """PRIORITIES: today's top 3 with their status, or how to set them."""
    import priorities as PR
    pr = m.get("priorities") or {}
    e = pr.get("today") or {}
    items = e.get("items") or []
    wk = pr.get("week") or {}
    head = T(("PRIORITIES", "bold " + C["activity"]))
    if wk.get("set_days"):
        head.append("  {} of {} done this week · {}-day streak".format(wk["done"], wk["items"], wk["streak"]), style=C["muted"])
    out = [fit(head, w)]
    if not items:
        out += K.para("None set yet. What are today's top 3? fitdash priorities set \"…\" \"…\" \"…\"",
                      w, C["muted"], indent=2, prefix=Text("  "))
    else:
        col = {"done": C["good"], "partial": C["watch"], "missed": C["flag"], None: C["ink2"]}
        for k, it in enumerate(items, 1):
            st = it.get("status")
            tail = "" if st is None else " · " + PR.LABEL[st]
            out += K.para("{}. {}".format(k, it["text"]) + tail, w, C["ink"] if st is None else C["ink2"], indent=6,
                          prefix=T(("  {} ".format(PR.GLYPH[st]), "bold " + col[st])))
        if pr.get("prompt") == "review":
            out.append(fit(T(("  How did they go? fitdash priorities", C["muted"])), w))
        if e.get("reflection"):
            out += K.para("“{}”".format(e["reflection"]), w, C["muted"], indent=4, prefix=Text("    "))
    out.append(blank())
    return out


def _coach_lines(m: dict, w: int) -> list[Text]:
    """COACH · <kind> · <time>, then the note. A rule-based note is marked "auto"."""
    import coach as CO
    co = m.get("coach") or {}
    note = co.get("note")
    if note:
        at = datetime.fromisoformat(note["at"])
        head = "{} · {}".format(CO.KIND_TITLE.get(note.get("kind"), "Note"), at.strftime("%-I:%M %p").lower())
        text = note["text"]
    elif co.get("fallback"):
        head, text = "auto · no written note yet", co["fallback"]
    else:
        return []
    out = [fit(T(("COACH", "bold " + C["sleep"]), ("  " + head, C["muted"])), w)]
    out += K.para(text, w, C["ink"], indent=2, prefix=Text("  "))
    out.append(blank())
    return out


def sec_focus(m: dict, w: int) -> list[Text]:
    day = date.fromisoformat(m["date"])
    out: list[Text] = []
    title = T((day.strftime("%A, %B %-d"), "bold " + C["ink"]))
    sync = "synced " + _sync_label(m)
    if title.cell_len + 2 + len(sync) <= w:
        out.append(fit(title + Text(" " * (w - title.cell_len - len(sync))) + T((sync, C["muted"])), w))
    else:                                       # narrow window: the sync note gets its own line
        out += [fit(title, w)] + K.para(sync, w, C["muted"])
    out.append(blank())
    out += _rings_only(m, w)
    out.append(blank())

    # the one thing to know
    v = m["verdict"]
    word, _, rest = v["headline"].partition(":")
    out += K.para(rest.strip(), w, "bold " + C[v["level"]], indent=4,
                  prefix=T(("{} {} ".format(K.ICON[v["level"]], word.upper()), "bold reverse " + C[v["level"]]), "  "))
    out.append(blank())
    out += _coach_lines(m, w)
    out += _priority_lines(m, w)

    # key numbers: value, context, 14-day trend (today = last, bold)
    s, b = m["series"], m["baselines"]
    n = 14
    sl = m["sleep"]
    rec = m["recovery"]

    def avg(key, fmt):
        return "avg " + fmt(b[key]["mean"]) if b.get(key) else ""

    rows = [
        ("Recovery", "—" if rec["score"] is None else "{}%".format(rec["score"]),
         avg("recovery", lambda x: "{:.0f}%".format(x)) if rec["score"] is not None else rec["reason"] or "",
         K.recovery_dots(s["recovery"][-n:])),
        ("Sleep score", "—" if _sscore(sl) is None else str(_sscore(sl)),
         "{} · {} of need".format(hm(sl.get("asleep")), "{}%".format(sl["performance"])) if sl.get("performance") is not None
         else "no night recorded",
         K.spark(s["sleep_score"][-n:], C["sleep"], lo=0, hi=100)),
        ("Resting HR", "—" if s["rhr"][-1] is None else "{:.0f} bpm".format(s["rhr"][-1]), avg("rhr", lambda x: "{:.0f}".format(x)),
         K.spark(s["rhr"][-n:], C["heart"])),
        ("HRV", "—" if s["hrv"][-1] is None else "{:.0f} ms".format(s["hrv"][-1]), avg("hrv", lambda x: "{:.0f}".format(x)),
         K.spark(s["hrv"][-n:], C["good"])),
        ("Strain", "—" if m["strain"]["day"] is None else "{:.1f}".format(m["strain"]["day"]),
         ("so far · " if m["partial_day"] else "") + ("target {:.0f}–{:.0f}".format(*m["strain"]["target"]) if m["strain"]["target"] else ""),
         K.spark(s["strain"][-n:], K.strain_color, lo=0, hi=21)),
        ("Steps", "—" if s["steps"][-1] is None else "{:,.0f}".format(s["steps"][-1]),
         ("so far · " if m["partial_day"] else "") + "goal {:,}".format(m["config"].get("steps_goal") or 10000),
         K.spark(s["steps"][-n:], C["activity"], lo=0)),
    ]
    KEY_W = 63                                  # 12 label + 11 value + 25 context + "14 days → today"
    side_by_side = w >= KEY_W + 4 + 44
    kw = KEY_W if side_by_side else w
    sw = w - KEY_W - 4 if side_by_side else w
    show_spark = kw >= 48 + n                   # label + value + context + one cell per day
    spark_head = "14 days → today" if kw >= KEY_W else "{} days".format(n)
    key: list[Text] = [fit(T(("{:<12}{:<11}{:<25}".format("", "today", ""), C["muted"]),
                             ((spark_head if show_spark else ""), C["muted"])), kw)]
    for lab, val, ctx, sp in rows:
        line = T(("{:<12}".format(lab), C["ink2"]), ("{:<11}".format(val), "bold " + C["ink"]), ("{:<25}".format(ctx[:23]), C["muted"]))
        if show_spark:
            line.append_text(sp)
        key.append(fit(line, kw))
    if show_spark:
        key.append(fit(T(("{:<48}".format(""), ""), ("●≥67 ◐ ○≤33", C["muted"])), kw))
    side = _attention_and_plan(m, sw)
    if side_by_side:
        for i in range(max(len(key), len(side))):
            left = key[i] if i < len(key) else Text("")
            line = left + Text(" " * (KEY_W - left.cell_len + 4))
            if i < len(side):
                line.append_text(side[i])
            out.append(fit(line, w))
    else:
        out += key + [blank()] + side
    return out


def _todays_plan(m: dict, w: int) -> list[Text]:
    """TODAY'S PLAN: a checklist of concrete actions (bedtime, next lift, steps, impact, strain)."""
    out = [fit(T(("TODAY'S PLAN", "bold " + C["ink2"])), w)]
    sl, tr, cfg = m["sleep"], m["training"], m["config"]
    tn = sl.get("tonight") or {}
    sp = tr.get("split") or {}
    items = []
    if tn.get("asleep_by") is not None:
        why = "{} need · {} debt to pay down".format(hm((tn.get("need") or {}).get("total")), hm(tn.get("debt")))
        ex = m.get("experiment")
        upcoming = [r for r in (ex or {}).get("nights", []) if r["state"] == "upcoming"]
        if upcoming:
            why += " · experiment night {} of {}: lights out by {} at the latest".format(
                ex["nights"].index(upcoming[0]) + 1, len(ex["nights"]), short_clock(D.hhmm(ex["lights_out"])))
        items.append(("Asleep by {}".format(short_clock(tn["asleep_by"])), why))
    elif cfg.get("bedtime_goal"):
        items.append(("In bed by {}".format(short_clock(D.hhmm(cfg["bedtime_goal"]))), "your bedtime goal"))
    if sp.get("next"):
        fresh = (sp.get("fresh") or {}).get(sp["next"])
        tip = "" if fresh is None else " · muscles {}% fresh".format(fresh) + (": go lighter or swap the order" if fresh < 70 else "")
        items.append(("Next lift: {}".format(sp["next"]), "gym {}/{} this week{}".format(tr["gym_week"], tr["gym_goal"], tip)))
    else:
        items.append(("Next lift: —", "tell Claude your last split day"))
    steps = (m.get("activity") or {}).get("steps") or 0
    goal = cfg.get("steps_goal") or 10000
    if m.get("partial_day") and steps < goal:
        items.append(("{:,} more steps".format(int(goal - steps)), "{:,} so far of {:,}".format(int(steps), goal)))
    if tr.get("impact_spike_recent"):
        items.append(("No running or jumping", "impact spike this or last week (knee)"))
    st = m["strain"]
    if st.get("target") and st.get("day") is not None:
        lo, hi = st["target"]
        state = "in range" if lo <= st["day"] <= hi else "below" if st["day"] < lo else "above"
        items.append(("Strain {:.0f}–{:.0f}".format(lo, hi), "{:.1f} so far, {}".format(st["day"], state)))
    for head, why in items:
        out += K.para(why, w, C["muted"], indent=6, prefix=T(("  □ ", C["ink2"]), (head, "bold " + C["ink"]), ("  ", "")))
    return out


def _attention_and_plan(m: dict, w: int) -> list[Text]:
    out: list[Text] = []
    att = _attention(m)
    out.append(fit(T(("NEEDS ATTENTION", "bold " + C["ink2"])), w))
    if att:
        for lvl, txt in att:
            out += K.status_lines(lvl, txt, w)
    else:
        out.append(K.status_chip("good", "Nothing flagged today", w))
    out.append(blank())

    out += _todays_plan(m, w)
    return out


# ---------------------------------------------------------------- lifting

SETS_SCALE = 25          # the sets bar spans 0-25 hard sets
LIFT_SHORT = {"romanian deadlift": "RDL", "overhead press": "OHP", "incline bench press": "incline bench",
              "triceps extension": "tri extension", "triceps pushdown": "pushdown", "dumbbell press": "DB press",
              "rear delt fly": "rear delt", "lat pulldown": "pulldown", "calf raise": "calves",
              "incline dumbbell press": "incline DB press", "single arm pushdown": "1-arm pushdown"}


def _sets_bar(v: float, lo: int, hi: int, cells: int) -> Text:
    """0-25 sets; the target range is drawn as a lighter track so you see where 10-20 sits."""
    col = C["good"] if lo <= v <= hi else C["watch"] if v < lo else C["strain"][3]
    t = Text(no_wrap=True)
    for i in range(cells):
        a, b = i * SETS_SCALE / cells, (i + 1) * SETS_SCALE / cells
        if v >= b:
            t.append("█", style=col)
        elif v > a:
            t.append(K.HBLOCKS[max(1, int((v - a) / (b - a) * 8))], style=col)
        else:
            inside = a < hi and b > lo
            t.append("░" if inside else "·", style=C["rule"] if inside else C["faint"])
    return t


GROUP_COLOR = {"push": "#e5704b", "pull": "#3987e5", "legs": "#c27ce6", "core": "#eda100"}
GROUP_NAME = {"push": "PUSH", "pull": "PULL", "legs": "LEGS", "core": "CORE"}


def _wt(e: dict) -> str:
    """A set's weight as typed: 50lb, 59kg, or bw."""
    if e.get("lb") is not None:
        return "{:g}lb".format(e["lb"])
    return "bw" if not e.get("kg") else "{:g}kg".format(round(e["kg"], 1))


def _target_bar(v: float, cells: int, color: str, lo: int, hi: int) -> Text:
    """One cell per set up to `cells` (20 at full width): the 10–20 target zone is a shaded track,
    below it a dotted one, so where a bar stops says how far from the zone it is."""
    scale = cells / hi
    t = Text(no_wrap=True)
    for i in range(cells):
        a, b = i / scale, (i + 1) / scale
        if v >= b:
            t.append("█", style=color)
        elif v > a:
            t.append(K.HBLOCKS[max(1, int((v - a) / (b - a) * 8))], style=color)
        elif a >= lo:
            t.append("░", style=C["rule"])
        else:
            t.append("·", style=C["faint"])
    t.append("▶" if v > hi else " ", style=color)
    return t


def _need(v: float, lo: int, hi: int) -> tuple[str, str]:
    if v > hi:
        return "{:g} over".format(round(v - hi, 1)), C["watch"]
    if v >= lo:
        return "✓ in range", C["good"]
    if v == 0:
        return "not trained", C["muted"]
    return "need {:g}".format(round(lo - v + 0.0001, 1) if (lo - v) % 1 else lo - v), C["ink2"]


def sec_lifting(m: dict, w: int) -> list[Text]:
    """This week's volume by muscle group against the 10–20 set target, the sessions behind it, and
    strength: progress charts for lifts done twice or more, best sets for the rest."""
    lf = m.get("lifting") or {}
    out: list[Text] = []
    if not lf.get("has_log"):
        out += headline(w, "none", ("No sets logged", "yet"))
        out += takeaway("Log a session in one line, e.g.  fitdash lift bench 3x8@60 row 4x10@50  "
                        "(pullups 3x8 for bodyweight, 25lb for pounds). Then this box shows hard sets per "
                        "muscle against the 10-20 a week that builds muscle, your progress on each lift, "
                        "and Muscle Freshness uses your real sets.", w)
        return out
    lo, hi = lf["target"]
    sets = lf["sets"]
    sessions = lf.get("sessions") or []
    in_range = [mu for mu in S.MUSCLES if lo <= sets[mu] <= hi]
    total_sets = sum(x["sets"] for x in sessions)
    tons = sum(x["tonnage"] for x in sessions)
    out += headline(w, "good" if len(in_range) >= 8 else "watch" if sessions else "none",
                    ("{}".format(len(sessions)), "session{} this week".format("" if len(sessions) == 1 else "s")),
                    ("{}".format(total_sets), "sets"), ("{:,} kg".format(tons), "lifted"),
                    ("{}/{}".format(len(in_range), len(S.MUSCLES)), "muscles in range"))
    groups = [(g, ms, sum(sets[mu] for mu in ms) / (len(ms) * lo)) for g, ms in lf.get("groups") or []]
    behind = [(g, ms) for g, ms, frac in groups if frac < 0.5 and g != "core"]
    if behind:
        nxt = ((m.get("training") or {}).get("split") or {}).get("next")
        out += takeaway("Behind this week: {}.{}".format(
            ", ".join("{} ({:g} sets)".format(GROUP_NAME[g].lower(), round(sum(sets[mu] for mu in ms), 1)) for g, ms in behind),
            " Next session: {}.".format(nxt) if nxt else ""), w)
    out.append(blank())

    # ---- volume vs target, grouped
    cells = 20 if w >= 70 else 10
    name_w = 12
    head = Text(" " * (4 + name_w), no_wrap=True)
    axis = [" "] * (cells + 1)
    for v in (0, lo, hi):
        i = min(cells - len(str(v)) + 1, int(round(v / hi * cells)))
        axis[i:i + len(str(v))] = list(str(v))
    head.append("".join(axis), style=C["muted"])
    head.append("{:>6}  {}".format("sets", "status"), style=C["muted"])
    out.append(fit(T(("Hard sets per muscle · last 7 days · target {}–{}".format(lo, hi), "bold " + C["ink2"])), w))
    out.append(fit(head, w))
    for g, ms in lf.get("groups") or []:
        col = GROUP_COLOR[g]
        gsum = sum(sets[mu] for mu in ms)
        out.append(fit(T(("  " + GROUP_NAME[g], "bold " + col), ("  {:g} sets".format(round(gsum, 1)), C["muted"])), w))
        for mu in ms:
            v = sets[mu]
            word, wcol = _need(v, lo, hi)
            out.append(fit(T(("    {:<{}}".format(MUSCLE_NAMES[mu].lower(), name_w), C["ink2"] if v else C["muted"]),
                             _target_bar(v, cells, col, lo, hi),
                             (" {:>5}".format("{:g}".format(round(v, 1)) if v else "0"), "bold " + C["ink"] if v else C["muted"]),
                             ("  " + word, wcol)), w))
    out += _flow([T(("·", C["faint"]), (" under {}".format(lo), C["muted"])), T(("░", C["rule"]), (" the {}–{} target zone".format(lo, hi), C["muted"])),
                   T(("█", C["ink2"]), (" sets done", C["muted"]))], w, gap=3, indent=4)
    out.append(blank())

    # ---- sessions
    if sessions:
        out.append(sub("Sessions this week", w))
        for x in sorted(sessions, key=lambda x: x["date"], reverse=True):
            lab = x["label"]
            col = GROUP_COLOR.get(lab.split(" ")[0], C["ink2"]) if lab != "arms" else GROUP_COLOR["pull"]
            out.append(fit(T(("  {:<9}".format(sdate(x["date"])), C["ink2"]), ("{:<12}".format(lab.upper()), "bold " + col),
                             ("{:>3} sets".format(x["sets"]), C["ink"]), ("   {:,} kg lifted".format(x["tonnage"]), C["muted"])), w))
            tops = sorted(x["exercises"], key=lambda e: -(e["e1rm"] or 0))
            items = ["{} {}×{}".format(LIFT_SHORT.get(e["exercise"], e["exercise"]), _wt(e), e["reps"]) for e in tops]
            out += K.para(" · ".join(items), w, C["muted"], indent=4, prefix=Text("    "))
        out.append(blank())

    # ---- strength
    lifts = lf.get("lifts") or []
    trending = [r for r in lifts if len(r["points"]) >= 2]
    if trending:
        out.append(sub("Getting stronger? · est. 1RM, 8 weeks", w))
        ex_w = min(20, max(12, w - 46))
        for r in trending[:6]:
            vals = [p["e1rm"] for p in r["points"]][-10:]
            chg = r["change_pct"] or 0
            name = r["exercise"] if len(r["exercise"]) < ex_w else LIFT_SHORT.get(r["exercise"], r["exercise"])
            out.append(K.fit_parts(w, T(("  {:<{}} ".format(_short(name, ex_w - 1), ex_w - 1), C["ink2"]),
                                        K.spark(vals, C["strain"][4]), (" " * (11 - len(vals)), ""),
                                        ("{:>5.0f} kg".format(vals[-1]), "bold " + C["ink"]),
                                        ("  {:+.1f}%".format(chg), C["good"] if chg > 0 else C["watch"] if chg < 0 else C["ink2"]),
                                        ("  ★ PR" if r["pr"] else "", "bold " + C["good"]))))
        out.append(blank())
    best = [b for b in lf.get("best_sets") or [] if not any(t["exercise"] == b["exercise"] for t in trending)]
    if best:
        out.append(sub("Best sets so far" + (" · progress charts start from the 2nd session of a lift" if not trending else ""), w))
        col_w = (w - 2) // 2 if w >= 80 else w - 2
        cells_ = []
        for b in best[:10]:
            name = LIFT_SHORT.get(b["exercise"], b["exercise"])
            cells_.append(T(("{:<16}".format(_short(name, 15)), C["ink2"]), ("{:>7}×{:<3}".format(_wt(b), b["reps"]), "bold " + C["ink"]),
                            ("{:>4.0f} kg 1RM".format(b["e1rm"]), C["muted"])))
        for i in range(0, len(cells_), 2 if col_w < w - 2 else 1):
            row = Text("  ", no_wrap=True)
            row.append_text(cells_[i])
            if col_w < w - 2 and i + 1 < len(cells_):
                row.append(" " * max(2, col_w - cells_[i].cell_len))
                row.append_text(cells_[i + 1])
            out.append(fit(row, w))
        out.append(blank())
    out += details([("sets", "a hard set counts 1 for the main muscle, ½ for helpers"), ("1RM", "estimated from your best set"),
                    ("deep dive", "fitdash --section lifting")], w)
    return out


# ---------------------------------------------------------------- journal & habits

def _habit_row(r: dict, ndays: int, label_w: int, col: str, w: int) -> Text:
    """● did it (green for a habit to do, red for one to avoid), ○ didn't, · not logged."""
    line = T(("  {:<{}}".format(_short(r["label"], label_w - 1), label_w), C["ink2"]))
    for c in r["cells"][-ndays:]:
        line.append(("●" if c else "○" if c is False else "·") + " ",
                    style=col if c else C["muted"] if c is False else C["faint"])
    line.append("  {:>2}".format("{}".format(r["yes7"]) if r["logged7"] else "–"), style="bold " + C["ink"])
    e = r.get("effect")
    if e and e["strength"] != "unclear":
        line.append("   {:+.0f} rec".format(e["diff"]), style="bold " + (C["good"] if e["diff"] > 0 else C["flag"]))
        if line.cell_len + len(e["strength"]) + 1 <= w:
            line.append(" " + e["strength"], style=C["muted"])
    elif e and line.cell_len + 13 <= w:
        line.append("   {:+.0f} unclear".format(e["diff"]), style=C["muted"])
    return K.fit_parts(w, line)


def sec_journal(m: dict, w: int) -> list[Text]:
    """14-day habit grid (● did it, ○ didn't, · not logged), 7-day counts, and each habit's link to the
    next morning's Recovery once there's enough data."""
    j = m.get("journal") or {}
    out: list[Text] = []
    if not j.get("has_log"):
        out += headline(w, "none", ("No journal yet", ""))
        rows = j.get("rows") or []
        ex = ["+" + r["key"] for r in rows if r.get("good") is True][:1] + \
             ["-" + r["key"] for r in rows if r.get("good") is not True][:2]
        out += takeaway('Log a day in one line: fitdash journal {} note "…", or run '.format(" ".join(ex)) +
                        "fitdash journal to be asked each habit. After about 5 days with and 5 without a habit, "
                        "its link to your next-morning Recovery shows up here and in What drives your recovery.", w)
        return out
    rows, days = j["rows"], j["days"]

    def on_track(r, c):
        """True when that day went the right way: did a habit to do, or skipped one to avoid."""
        if c is None or r["good"] is None:
            return None
        return c if r["good"] else not c

    day_scores = []
    for i in range(len(days)):
        outcomes = [on_track(r, r["cells"][i]) for r in rows if on_track(r, r["cells"][i]) is not None]
        day_scores.append(None if not outcomes else sum(outcomes) / len(outcomes))
    week = [x for x in day_scores[-7:] if x is not None]
    out += headline(w, "good" if week and statistics.fmean(week) >= 0.7 else "watch" if week else "none",
                    ("{:.0f}%".format(100 * statistics.fmean(week)) if week else "—", "of habits on track, last 7 days"),
                    ("{}/7".format(j["logged7"]), "days logged"), ("{}".format(j["streak"]), "day streak"))
    if not j["today_logged"] and not j["yesterday_logged"]:
        out += takeaway("Yesterday isn't logged yet: fitdash journal yesterday", w)
    out.append(blank())
    label_w = min(22, max(12, max(len(r["label"]) for r in rows) + 1))
    n = len(days) if w >= label_w + 2 + 2 * len(days) + 10 else 7
    head = T(("  " + " " * label_w, ""))
    for x in days[-n:]:
        head.append(date.fromisoformat(x).strftime("%a")[0] + " ", style=C["muted"])
    head.append("  7 days", style=C["muted"])
    out.append(fit(head, w))
    score_row = T(("  {:<{}}".format("day score", label_w), "bold " + C["ink2"]))
    for v in day_scores[-n:]:
        score_row.append(("·" if v is None else K.EIGHTHS[max(1, min(8, int(round(v * 8))))]) + " ",
                         style=C["faint"] if v is None else K.ramp([C["flag"], C["watch"], C["good"]], v))
    out.append(fit(score_row, w))
    out.append(blank())
    for good, title, col, word in ((True, "TO DO", C["good"], "done"), (False, "TO AVOID", C["flag"], "slips"),
                                   (None, "TRACKING", C["strain"][4], "times")):
        group = [r for r in rows if r["good"] is good]
        if not group:
            continue
        hits = sum(r["yes7"] for r in group)
        out.append(fit(T(("  " + title, "bold " + col),
                         ("  {} {} in the last 7 days".format(hits, word) if any(r["logged7"] for r in group) else "", C["muted"])), w))
        for r in group:
            row = T(("  {:<{}}".format(_short(r["label"], label_w - 1), label_w), C["ink2"]))
            for c in r["cells"][-n:]:
                o = on_track(r, c)
                if c is None:
                    row.append("· ", style=C["faint"])
                elif o is None:
                    row.append("■ " if c else "□ ", style=C["strain"][4])
                else:
                    row.append("■ ", style=C["good"] if o else C["flag"])
            ok = sum(1 for c in r["cells"][-7:] if on_track(r, c)) if r["good"] is not None else r["yes7"]
            row.append("  {}/{}".format(ok, r["logged7"]) if r["logged7"] else "  –", style="bold " + C["ink"])
            out.append(fit(row, w))
        out.append(blank())
    out += _flow([T(("■", C["good"]), (" on track (did it / avoided it)", C["muted"])), T(("■", C["flag"]), (" off track", C["muted"])),
                  T(("·", C["faint"]), (" not logged", C["muted"]))], w, gap=3, indent=2)
    if j.get("last_note"):
        d_, note = j["last_note"]
        out.append(blank())
        label = date.fromisoformat(d_).strftime("%a %-d %b")
        out += K.para("“{}”".format(note), w, C["ink2"], indent=4, prefix=T(("  " + label + "  ", C["muted"])))
    return out


def _short(label: str, n: int) -> str:
    return label if len(label) <= n else label[:n - 1] + "…"


def sec_insights(m: dict, w: int) -> list[Text]:
    """What tends to come before your better and worse recovery mornings: each habit's effect as a
    thick bar from 0, with its 95 % range drawn thin around it. A range that crosses the center line
    could be chance. Correlation, not proof."""
    ins = m.get("insights") or {}
    effects = ins.get("effects") or []
    out: list[Text] = []
    if ins.get("n_days", 0) < ins.get("min_days", 21):
        out += headline(w, "none", ("{} days".format(ins.get("n_days", 0)), "of Recovery so far"))
        out += takeaway("Patterns need about {} mornings with a Recovery score; this fills in on its own."
                        .format(ins.get("min_days", 21)), w)
        return out
    real = [e for e in effects if e["strength"] != "unclear"]
    if real:
        top = real[0]
        out += headline(w, "good" if top["diff"] > 0 else "watch",
                        ("{:+.0f}".format(top["diff"]), "Recovery when " + top["label"]),
                        ("{} vs {}".format(top["mean_yes"], top["mean_no"]), "on average"))
    else:
        out += headline(w, "none", ("No clear pattern", "yet in {} mornings".format(ins["n_days"])))
    out.append(blank())
    lw = max(14, min(w - 40, max((len(e["label"]) for e in effects), default=20) + 1))
    plot_w = w - lw - 15                             # "  " + label + plot + "  with vs w/o"
    plot_w -= (plot_w + 1) % 2                       # odd, so 0 has its own column
    span = 30.0

    def X(v: float) -> int:
        return int(round((max(-span, min(span, v)) + span) / (2 * span) * (plot_w - 1)))

    axis = [" "] * plot_w
    for v, lab in ((-30, "-30"), (0, "0"), (30, "+30")):
        i = X(v) - (len(lab) // 2 if v == 0 else 0 if v < 0 else len(lab) - 1)
        axis[i:i + len(lab)] = list(lab)
    head = "next-morning Recovery when…" if lw >= 28 else "Recovery when…"
    out.append(fit(T(("  {:<{}}".format(_short(head, lw), lw), C["muted"]), ("".join(axis), C["muted"]),
                     ("  with vs w/o", C["muted"])), w))
    zero = X(0)
    for e in effects:
        se = abs(e["diff"] / e["t"]) if e.get("t") else span
        lo, hi = e["diff"] - 1.96 * se, e["diff"] + 1.96 * se
        clear = e["strength"] != "unclear"
        col = (C["good"] if e["diff"] > 0 else C["flag"]) if clear else C["rule"]
        a, b_ = sorted((zero, X(e["diff"])))
        row = Text(no_wrap=True)
        for i in range(plot_w):
            v = i / (plot_w - 1) * 2 * span - span
            if a <= i <= b_ and i != zero and e["diff"] != 0:
                row.append("█", style=col)
            elif i == zero:
                row.append("│", style=C["rule"])
            elif lo <= v <= hi:
                row.append("─", style=col if clear else C["faint"])
            else:
                row.append(" ")
        out.append(fit(T(("  {:<{}}".format(_short(e["label"], lw - 1), lw), C["ink"] if clear else C["muted"]), row,
                         ("  {:>3} vs {:<3}".format(e["mean_yes"], e["mean_no"]), C["ink2"] if clear else C["muted"])), w))
    out.append(blank())
    out += _flow([T(("█", C["good"]), (" better", C["muted"])), T(("█", C["flag"]), (" worse", C["muted"])),
                  T(("─", C["ink2"]), (" 95% range: if it crosses │ it could be chance", C["muted"])),
                  T(("█", C["rule"]), (" unclear", C["muted"]))], w, gap=3, indent=2)
    out += K.para("Average Recovery the next morning with vs without each habit, over {} mornings since {}. "
                  "Correlation in your own data, not proof: hard days, late nights and short sleep often come together, "
                  "and Recovery includes the sleep score, so the sleep rows partly measure themselves."
                  .format(ins["n_days"], sdate(ins["since"])), w, C["muted"], indent=2, prefix=Text("  "))
    return out


# ---------------------------------------------------------------- layout

MAX_W = 100        # a single column never gets wider than this: long lines are hard to scan
TWO_COL_MIN = 140  # from here, boxes sit in two columns (each at least 69 wide)
COLUMNS = (["sleep", "experiment", "recovery", "insights", "journal", "body"],      # left: recovery side
           ["muscles", "lifting", "training", "workouts", "strain"])    # right: training side, muscle building first

PANELS = {
    "sleep": ("Sleep", sec_sleep, "sleep"),
    "experiment": ("Sleep experiment", sec_experiment, "sleep"),
    "recovery": ("Recovery & heart", sec_recovery, "good"),
    "strain": ("Strain & activity", sec_strain, None),
    "workouts": ("Workouts · last 7 days", sec_workouts, None),
    "muscles": ("Muscle Freshness", None, None),      # drawn by freshness_panel (its own card style)
    "training": ("Training plan", sec_training, "activity"),
    "body": ("Body & nutrition", sec_body, "body"),
    "week": ("This week vs last", sec_week, "muted"),
    "lifting": ("Lifting · sets & progress", sec_lifting, "activity"),
    "insights": ("What drives your recovery", sec_insights, "good"),
    "journal": ("Journal & habits", sec_journal, "sleep"),
    "logs": ("Logs", sec_logs, "muted"),
}
ORDER = COLUMNS[0][:-1] + COLUMNS[1] + ["week"] + COLUMNS[0][-1:]   # single column: recovery, training, the week, body


def panel(title: str, lines: list[Text], width: int, color: str | None) -> Panel:
    col = C["strain"][3] if color is None else C.get(color, color)
    return Panel(Group(*lines), title=Text(" " + title + " ", style="bold " + C["ink"]), title_align="left",
                 box=box.ROUNDED, border_style=col, padding=(0, 1), width=width)


def _box_height(k: str, m: dict, width: int, sort: str = "default") -> int:
    buf = io.StringIO()
    Console(width=width, file=buf, no_color=True, color_system=None, highlight=False).print(box_for(k, m, width, False, sort))
    return len(buf.getvalue().splitlines())


def box_for(k: str, m: dict, width: int, color: bool, sort: str = "default") -> Panel:
    if k == "muscles":
        return freshness_panel(m, width, color, sort)
    title, fn, col = PANELS[k]
    return panel(title, fn(m, width - 4), width, col)


def render(m: dict, console: Console, width: int, section: str | None = None, full: bool = False,
           sort: str = "default") -> None:
    """Default: the Today box, then every section in its own box. `section` shows just one."""
    color = console.color_system is not None and not console.no_color
    bw = min(width, MAX_W)
    if not m.get("has_data"):
        console.print(panel("fitdash", nodata_lines(bw - 4, "No data to show: {}.".format(m.get("empty_reason", "empty")), 0) +
                            nodata_lines(bw - 4, "Connect and import with the fitbit-local tools, then run this again.", 0),
                            bw, "muted"))
        return
    inner = bw - 4
    if m.get("period"):
        console.print(panel("Today", sec_focus(m, inner), bw, None))
        console.print(panel("{} view".format(m["period"]["period"].title()), sec_period(m, inner), bw, None))
        console.print(panel("Data", sec_footer(m, inner), bw, "muted"))
        return
    if section in ("overview", "today"):
        console.print(panel("Today", sec_focus(m, inner), bw, None))
        return
    if section in ("muscles", "freshness"):
        console.print(box_for("muscles", m, min(width, FRESH_MAX_W), color, sort))
        _deep_boxes(m, console, section, bw)
        return
    if section:
        for k in [section] + (["experiment"] if section == "sleep" else []):
            console.print(box_for(k, m, bw, color, sort))
        _deep_boxes(m, console, section, bw)
        return
    if width >= TWO_COL_MIN:
        col_w = min(MAX_W, (width - 1) // 2)
        total = 2 * col_w + 1
        console.print(panel("Today", sec_focus(m, total - 4), total, None))
        g = Table.grid(padding=(0, 1))
        g.add_column(width=col_w)
        g.add_column(width=col_w)
        cols = [list(keys) for keys in COLUMNS]
        # the week-over-week box goes to whichever column is shorter, so neither leaves a big empty gap
        heights = [sum(_box_height(k, m, col_w, sort) for k in keys) for keys in cols]
        cols[heights.index(min(heights))].append("week")
        stacks = [Group(*(box_for(k, m, col_w, color, sort) for k in keys)) for keys in cols]
        g.add_row(*stacks)
        console.print(g)
        console.print(panel("Data", sec_footer(m, total - 4), total, "muted"))
        return
    console.print(panel("Today", sec_focus(m, inner), bw, None))
    for k in ORDER:
        console.print(box_for(k, m, bw, color, sort))
    console.print(panel("Data", sec_footer(m, inner), bw, "muted"))


def _deep_boxes(m: dict, console: Console, section: str, bw: int) -> None:
    """A single section gets an extended view: more history and extra charts (deep.py)."""
    if not m.get("deep"):
        return
    import deep as DP
    fn = DP.DEEP.get(section)
    for title, lines, col in (fn(m, bw - 4) if fn else []):
        console.print(panel(title, lines, bw, col))


def make_console(width: int, no_color: bool, file=None, force_terminal: bool | None = None) -> Console:
    no_color = no_color or "NO_COLOR" in os.environ
    return Console(width=width, file=file, no_color=no_color, color_system=None if no_color else "auto",
                   force_terminal=force_terminal, highlight=False, emoji=False, soft_wrap=False)


def tag(argv: list[str]) -> int:
    """fitdash tag <date> <split day>: record what a strength session trained.

    <date> is YYYY-MM-DD, "today" or "yesterday". <split day> matches your split by keyword
    (push, pull, legs, arms, …). Writes only ~/.fitbit-mcp/training_log.json."""
    if len(argv) != 2:
        print("usage: fitdash tag <YYYY-MM-DD|today|yesterday> <split day>", file=sys.stderr)
        return 2
    when, what = argv
    today = datetime.now(D.NY).date()
    try:
        d = {"today": today, "yesterday": today - timedelta(days=1)}.get(when) or date.fromisoformat(when)
    except ValueError:
        print("not a date: {}".format(when), file=sys.stderr)
        return 2
    split = D.load_config().get("split") or []
    matches = [x for x in split if what.lower() in x.lower()]
    if len(matches) != 1:
        print("'{}' matches {} of your split days: {}".format(what, len(matches), ", ".join(split)), file=sys.stderr)
        return 2
    path = D.training_log_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("sessions", {})[d.isoformat()] = matches[0]
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print("tagged {} as {}".format(d.isoformat(), matches[0]))
    return 0


REPO = Path(__file__).resolve().parents[4]          # .claude/skills/stats/scripts → the fitbit-mcp repo
SYNC_MAX_AGE_MIN = float(os.environ.get("FITDASH_SYNC_MAX_AGE", "30"))


def _minutes_since_sync() -> float | None:
    store = D.open_store()
    if store is None:
        return None
    try:
        at, _ = store.last_sync()
    finally:
        store.close()
    if not at:
        return None
    t = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return (datetime.now(t.tzinfo) - t).total_seconds() / 60


def auto_sync(force: bool = False) -> None:
    """Refresh the local database before drawing, when it's stale. Runs the project's own sync code
    (the same path as the MCP sync_data tool) in the project environment; never fails the dashboard."""
    import subprocess
    age = _minutes_since_sync()
    if not force and age is not None and age < SYNC_MAX_AGE_MIN:
        return
    if not (REPO / "pyproject.toml").exists():
        print("  sync skipped: fitbit-mcp project not found at {}".format(REPO), file=sys.stderr)
        return
    print("  last sync {}; refreshing…".format("never" if age is None else
          "{:.0f} min ago".format(age) if age < 120 else "{:.0f} h ago".format(age / 60)), file=sys.stderr)
    try:
        subprocess.run(["uv", "run", "--quiet", "--frozen", "--directory", str(REPO), "python",
                        str(Path(__file__).with_name("sync_now.py"))], timeout=240, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("  sync skipped: {}".format(exc), file=sys.stderr)


def lift(argv: list[str]) -> int:
    """fitdash lift <exercise> 3x8@60 …: log sets to ~/.fitbit-mcp/lift_log.json (see lifts.py)."""
    import lifts as L
    if argv[:1] in (["-h"], ["--help"]):
        print(L.__doc__)
        return 0
    return L.cli(argv, D.data_home(), datetime.now(D.NY))


def journal(argv: list[str]) -> int:
    """fitdash journal [day] +habit -habit note "…": the daily journal (see journal.py)."""
    import journal as J
    if argv[:1] in (["-h"], ["--help"]):
        print(J.__doc__)
        return 0
    return J.cli(argv, D.data_home(), D.load_config(), datetime.now(D.NY), sys.stdin.isatty() and sys.stdout.isatty())


def coach(argv: list[str]) -> int:
    """fitdash coach run|set|show|--install|--uninstall (see coach.py)."""
    import coach as CO
    import web as W
    if not argv or argv[0] in ("-h", "--help"):
        print(CO.__doc__)
        return 0
    home = D.data_home()
    now = datetime.now(D.NY)
    if argv[0] == "--install":
        return CO.install(home, W._fitdash(), W._path_env())
    if argv[0] == "--uninstall":
        return CO.uninstall()
    if argv[0] == "show":
        notes = [n for n in CO.load(home) if n["date"] == CO.coach_day(now)[0]]
        for n in notes:
            print("{} {} ({}): {}".format(n["at"][11:], CO.KIND_TITLE.get(n["kind"], n["kind"]), n["source"], n["text"]))
        if not notes:
            print("no notes today")
        return 0
    if argv[0] == "set":
        if len(argv) < 3 or argv[1] not in CO.KINDS + ("note",):
            print('usage: fitdash coach set <morning|activity|evening|note> "text"', file=sys.stderr)
            return 2
        CO.add(home, argv[1], " ".join(argv[2:]), now, "manual")
        _refresh_page()
        print("saved")
        return 0
    if argv[0] != "run":
        print("unknown coach command: {}".format(argv[0]), file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="fitdash coach run")
    ap.add_argument("--kind", choices=CO.KINDS)
    ap.add_argument("--force", action="store_true", help="write a note even if none is due")
    ap.add_argument("--dry-run", action="store_true", help="print the context and the note; save nothing")
    ap.add_argument("--no-sync", action="store_true")
    args = ap.parse_args(argv[1:])
    return coach_run(kind=args.kind, force=args.force, dry_run=args.dry_run, sync=not args.no_sync)


def coach_run(kind: str | None = None, force: bool = False, dry_run: bool = False, sync: bool = True,
              quiet: bool = False) -> int:
    import coach as CO
    home = D.data_home()
    if sync:
        auto_sync(force=False)
    now = datetime.now(D.NY)
    cfg = D.load_config()
    day = date.fromisoformat(CO.coach_day(now)[0])     # after midnight, the evening is still yesterday's
    store = D.open_store()
    try:
        m = D.build_model(store, day, cfg, now=now)
    finally:
        if store:
            store.close()
    notes = CO.load(home)
    found = CO.due(m, notes, now)
    workout = None
    if kind:
        if found and found[0] == kind:
            workout = found[1]
        elif kind == "activity":
            ws = sorted(CO._workouts_today(m), key=lambda w: w["end"])
            workout = ws[-1] if ws else None
            if not workout:
                print("no workout today to write about", file=sys.stderr)
                return 1
        if not force and not (found and found[0] == kind):
            if not quiet:
                print("no {} note due (use --force)".format(kind))
            return 0
    elif found:
        kind, workout = found
    elif force:
        kind = {"day": "morning"}.get(CO.kind_for(now), CO.kind_for(now))
    else:
        if not quiet:
            print("{} no note due".format(now.strftime("%H:%M")))
        return 0
    try:
        profile = (home / "coaching" / "profile.md").read_text()
    except OSError:
        profile = ""
    earlier = [n for n in notes if n.get("date") == CO.coach_day(now)[0]]
    ctx = CO.context(m, kind, workout, now, profile, earlier)
    if dry_run:
        print(CO.prompt(ctx))
        print("---")
    text = CO.generate(ctx)
    if text is None:
        print("{} {} note: Claude CLI unavailable or failed; the Today box keeps the rule-based note"
              .format(now.strftime("%H:%M"), kind), file=sys.stderr)
        return 1
    if dry_run:
        print(text)
        return 0
    CO.add(home, kind, text, now, "claude", ("workout:" + workout["start"]) if workout else None)
    _refresh_page()
    print("{} {} note: {}".format(now.strftime("%H:%M"), kind, text))
    return 0


def _refresh_page() -> None:
    """Re-render the phone page (if it's set up) so a new note shows there right away."""
    import web as W
    page = W.default_path(D.data_home())
    if not page.exists():
        return
    now = datetime.now(D.NY)
    store = D.open_store()
    try:
        m = D.build_model(store, now.date(), D.load_config(), now=now)
    finally:
        if store:
            store.close()
    W.write(page, W.to_html(lambda con: render(m, con, W.PHONE_WIDTH), W.PHONE_WIDTH))


def app(argv: list[str]) -> int:
    """fitdash app [--port N]: serve the phone app on 127.0.0.1 (see appserver.py)."""
    import appserver as A
    ap = argparse.ArgumentParser(prog="fitdash app", description=A.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)
    sync_cmd = None
    if (REPO / "pyproject.toml").exists():
        sync_cmd = ["uv", "run", "--quiet", "--frozen", "--directory", str(REPO), "python",
                    str(Path(__file__).with_name("sync_now.py"))]
    return A.serve(D.data_home(), args.port, sync_cmd)


def web(argv: list[str]) -> int:
    """fitdash web --install | --uninstall | --status: the phone page (see web.py)."""
    import web as W
    ap = argparse.ArgumentParser(prog="fitdash web", description=W.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--every", type=int, default=30, help="minutes between refreshes (default 30)")
    args = ap.parse_args(argv)
    if args.install:
        return W.install(D.data_home(), max(5, args.every))
    return W.uninstall() if args.uninstall else W.status(D.data_home())


def brief(argv: list[str]) -> int:
    """fitdash brief: sync, then a macOS notification with today's recovery, sleep and session."""
    import brief as B
    ap = argparse.ArgumentParser(prog="fitdash brief", description=B.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", dest="print_only", action="store_true", help="print only, no notification")
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--install", nargs="?", const="", metavar="HH:MM", help="schedule it daily")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    cfg = D.load_config()
    if args.uninstall:
        return B.uninstall()
    if args.status:
        return B.status()
    if args.install is not None:
        return B.install(args.install or B.default_time(cfg), D.data_home())
    if not args.no_sync:
        auto_sync(force=True)
        coach_run(sync=False, quiet=True)          # a morning note, if one is due, goes in the brief
    now = datetime.now(D.NY)
    cfg = D.load_config()
    store = D.open_store()
    try:
        m = D.build_model(store, now.date(), cfg, now=now)
    finally:
        if store:
            store.close()
    title, body = B.compose(m)
    note = (m.get("coach") or {}).get("note")
    if note and note.get("kind") == "morning":
        body = note["text"]
    print("{}  {}\n  {}".format(now.strftime("%Y-%m-%d %H:%M"), title, body))
    if not args.print_only and not B.notify(title, body):
        print("  (notification not shown: needs macOS osascript)", file=sys.stderr)
    return 0


SUBCOMMANDS = ("tag", "lift", "journal", "j", "brief", "web", "coach", "app", "priorities", "p")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].startswith("--") and argv[0][2:] in SUBCOMMANDS:
        argv = [argv[0][2:]] + argv[1:]          # `fitdash --priorities …` works like `fitdash priorities …`
    if argv[:1] == ["tag"]:
        return tag(argv[1:])
    if argv[:1] == ["lift"]:
        return lift(argv[1:])
    if argv[:1] in (["journal"], ["j"]):
        return journal(argv[1:])
    if argv[:1] == ["brief"]:
        return brief(argv[1:])
    if argv[:1] == ["web"]:
        return web(argv[1:])
    if argv[:1] == ["coach"]:
        return coach(argv[1:])
    if argv[:1] == ["app"]:
        return app(argv[1:])
    if argv[:1] in (["priorities"], ["p"]):
        import priorities as PR
        if argv[1:2] in (["-h"], ["--help"]):
            print(PR.__doc__)
            return 0
        return PR.cli(argv[1:], D.data_home(), datetime.now(D.NY), sys.stdin.isatty() and sys.stdout.isatty())
    ap = argparse.ArgumentParser(prog="fitdash", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--section", choices=SECTIONS)
    ap.add_argument("--period", choices=("day", "week", "month"), default="day")
    ap.add_argument("--days", type=int, help="trend window in days (14-90; default 28, or 56 for one --section)")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, local timezone)")
    ap.add_argument("--full", "--compact", action="store_true", help=argparse.SUPPRESS)   # old flags; the default shows everything
    ap.add_argument("--width", type=int)
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--json", action="store_true", help="print the computed model instead of charts")
    ap.add_argument("--html", nargs="?", const="", metavar="PATH",
                    help="write the dashboard as a phone-friendly web page (default ~/.fitbit-mcp/web/index.html)")
    ap.add_argument("--sync", action="store_true", help="sync from Google first, even if the data is fresh")
    ap.add_argument("--no-sync", action="store_true",
                    help="don't sync; by default fitdash syncs first when run in a terminal and the last sync is "
                         "over {:.0f} min old".format(SYNC_MAX_AGE_MIN))
    ap.add_argument("--sort", choices=("default", "freshness"), default="default",
                    help="muscle freshness cards: least-recovered first with 'freshness'")
    args = ap.parse_args(argv)

    # Auto-refresh only for an interactive look at today: not when piped (Claude syncs through the MCP
    # tools first), not for --json, and not for a past --date.
    interactive = sys.stdout.isatty() and not args.json and not args.date
    # the phone page is refreshed in the background: it syncs when stale, like a look in the terminal
    if args.sync or ((interactive or args.html is not None) and not args.no_sync and not args.date):
        auto_sync(force=args.sync)
    # morning: ask for today's top 3; evening: ask how they went (only in a terminal, once a day)
    if interactive and sys.stdin.isatty() and args.section in (None, "today", "overview") and args.period == "day":
        import priorities as PR
        if PR.prompts_enabled(D.load_config()):
            now_ = datetime.now(D.NY)
            kind = PR.due_prompt(PR.load(D.data_home()), now_)
            if kind:
                PR.run_prompt(D.data_home(), now_, kind)
    day = date.fromisoformat(args.date) if args.date else datetime.now(D.NY).date()
    width = args.width or shutil.get_terminal_size((100, 40)).columns
    width = max(60, width)
    store = D.open_store()
    try:
        m = D.build_model(store, day, D.load_config(), days=max(14, min(90, args.days or (56 if args.section else 28))), period=args.period)
    finally:
        if store:
            store.close()
    if args.json:
        print(json.dumps(m, default=str, indent=1))
        return 0
    if args.html is not None:
        import web as W
        out = Path(args.html).expanduser() if args.html else W.default_path(D.data_home())
        w = args.width or W.PHONE_WIDTH
        W.write(out, W.to_html(lambda con: render(m, con, w, args.section, sort=args.sort), w))
        print("wrote {}".format(out))
        return 0
    render(m, make_console(width, args.no_color), width, args.section, sort=args.sort)
    for msg in K.OVERFLOWS:
        print("OVERFLOW " + msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
