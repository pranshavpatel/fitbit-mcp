"""Unicode chart primitives that return rich Text lines no wider than the width they're given.

Every chart encodes meaning with shape as well as color (glyphs, position, labels), so it stays
readable when color is stripped: NO_COLOR, --no-color, or Claude Code's tool output.
"""
from __future__ import annotations

import math
import os
from typing import Callable, Sequence

from rich.text import Text

STRICT = os.environ.get("STATS_STRICT") == "1"   # tests: overflowing a width is a bug, not a crop
REPORT = os.environ.get("STATS_STRICT") == "report"
OVERFLOWS: list[str] = []

# Dark-terminal palette. Status colors are fixed and always paired with ✓ ! ✕.
C = {
    "ink": "#ecebe6", "ink2": "#c8c7bf", "muted": "#9a9893", "rule": "#5a5a55", "faint": "#34342f",
    "good": "#0ca30c", "watch": "#fab219", "flag": "#d03b3b", "none": "#898781",
    "recovery": {"green": "#0ca30c", "yellow": "#fab219", "red": "#d03b3b"},
    "strain": ["#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4"],
    "sleep": "#9085e9", "deep": "#5b4fd1", "rem": "#9085e9", "light": "#c4bdf6", "awake": "#e8a33d",
    "activity": "#eda100", "heat": ["#3a3018", "#5a4614", "#86640e", "#b58304", "#e0a000", "#fab219"],
    "body": "#c27ce6", "heart": "#e66767",
    "zone": {"light": "#5598e7", "fat_burn": "#eda100", "cardio": "#d95926", "peak": "#d03b3b"},
    "chip": {"STRENGTH_TRAINING": "#3987e5", "CARDIO_WORKOUT": "#d95926", "RUNNING": "#199e70",
             "TRAIL_RUN": "#199e70", "TREADMILL_RUNNING": "#199e70", "SOCCER": "#c98500", "WALKING": "#898781",
             "BOXING": "#d55181"},
}
ICON = {"good": "✓", "watch": "!", "flag": "✕", "none": "·"}
EIGHTHS = " ▁▂▃▄▅▆▇█"
HBLOCKS = " ▏▎▍▌▋▊▉█"
SPARK = "▁▂▃▄▅▆▇█"
STAGE_GLYPH = {"deep": "█", "light": "▓", "rem": "▒", "awake": "░"}
ZONE_GLYPH = {"light": "░", "moderate": "▒", "vigorous": "▓", "peak": "█"}
CHIP_LETTER = {"STRENGTH_TRAINING": "S", "CARDIO_WORKOUT": "C", "RUNNING": "R", "TRAIL_RUN": "R",
               "TREADMILL_RUNNING": "R", "SOCCER": "F", "WALKING": "W", "BOXING": "B"}


class Overflow(AssertionError):
    pass


def fit(t: Text, w: int) -> Text:
    """Hold a line to `w` cells. In strict (test) mode an overflow fails loudly."""
    t.no_wrap = True
    t.overflow = "ellipsis"
    if t.cell_len > w:
        msg = "line is {} cells, limit {}: {!r}".format(t.cell_len, w, t.plain)
        if STRICT:
            raise Overflow(msg)
        if REPORT:
            OVERFLOWS.append(msg)
        t.truncate(w, overflow="ellipsis")
    return t


def fit_parts(w: int, *parts) -> Text:
    """Join parts, dropping optional ones (marked with a leading None) from the end until it fits."""
    req = [p for p in parts if not (isinstance(p, tuple) and p and p[0] is None)]
    opt = [(p[1] if len(p) == 2 and isinstance(p[1], Text) else p[1:])
           for p in parts if isinstance(p, tuple) and p and p[0] is None]
    while True:
        t = T(*(req + opt))
        if t.cell_len <= w or not opt:
            return fit(t, w)
        opt.pop()


def T(*parts) -> Text:
    """T(("text", "style"), "plain", ...) → Text."""
    out = Text(no_wrap=True)
    for p in parts:
        if isinstance(p, Text):
            out.append_text(p)
        elif isinstance(p, tuple):
            out.append(p[0], style=p[1] or "")
        else:
            out.append(str(p))
    return out


def ramp(colors: Sequence[str], frac: float) -> str:
    frac = max(0.0, min(1.0, frac))
    return colors[min(len(colors) - 1, int(frac * len(colors)))]


def strain_color(v: float | None) -> str:
    return C["muted"] if v is None else ramp(C["strain"], v / 21)


def para(text: str, w: int, style: str, indent: int = 0, prefix: Text | None = None) -> list[Text]:
    """Word-wrap prose into lines of at most w cells; continuation lines are indented."""
    lines, cur = [], prefix.copy() if prefix is not None else Text(" " * indent, no_wrap=True)
    start_len = cur.cell_len
    for word in text.split():
        need = len(word) + (1 if cur.cell_len > start_len else 0)
        if cur.cell_len > start_len and cur.cell_len + need > w:
            lines.append(fit(cur, w))
            cur = Text(" " * indent, no_wrap=True)
            start_len = indent
        if cur.cell_len > start_len:
            cur.append(" ")
        cur.append(word[: max(1, w - cur.cell_len)], style=style)
    if cur.cell_len > start_len or not lines:
        lines.append(fit(cur, w))
    return lines


def status_lines(level: str, text: str, w: int) -> list[Text]:
    return para(text, w, C["ink2"], indent=2, prefix=T((ICON[level] + " ", "bold " + C[level])))


def status_chip(level: str, text: str, w: int) -> Text:
    return fit(T((ICON[level] + " ", "bold " + C[level]), (text, C["ink2"])), w)


# ---------------------------------------------------------------- big numbers & rings

DIGITS = {
    "0": ["█▀█", "█ █", "█▄█"], "1": ["▀█ ", " █ ", "▄█▄"], "2": ["▀▀█", "█▀▀", "█▄▄"],
    "3": ["▀▀█", " ▀█", "▄▄█"], "4": ["█ █", "▀▀█", "  █"], "5": ["█▀▀", "▀▀█", "▄▄█"],
    "6": ["█▀▀", "█▀█", "█▄█"], "7": ["▀▀█", "  █", "  █"], "8": ["█▀█", "█▀█", "█▄█"],
    "9": ["█▀█", "▀▀█", "▄▄█"], ".": [" ", " ", "▄"], "—": ["   ", "▀▀▀", "   "], "%": ["▀ ▄", " █ ", "▀ ▄"],
}


def big(s: str) -> list[str]:
    rows = ["", "", ""]
    for i, ch in enumerate(s):
        g = DIGITS.get(ch, ["   "] * 3)
        for r in range(3):
            rows[r] += ("" if i == 0 else " ") + g[r]
    return rows


_BRAILLE_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def ring(frac: float | None, color: str, value: str, unit: str, w: int = 20, h: int = 9) -> list[Text]:
    """A braille ring that fills clockwise from 12 o'clock, with a big number in the middle."""
    dw, dh = w * 2, h * 4
    cx, cy = (dw - 1) / 2, (dh - 1) / 2
    R = min(cx, cy)
    r_in = R - 2.6
    filled = [[0] * w for _ in range(h)]
    track = [[0] * w for _ in range(h)]
    f = 0.0 if frac is None else max(0.0, min(1.0, frac))
    for y in range(dh):
        for x in range(dw):
            dx, dy = x - cx, y - cy
            dist = math.hypot(dx, dy)
            if r_in <= dist <= R:
                ang = (math.atan2(dx, -dy) / (2 * math.pi)) % 1.0
                grid = filled if ang < f else track
                grid[y // 4][x // 2] |= _BRAILLE_BITS[y % 4][x % 2]
    rows = big(value)
    num_w = len(rows[0])
    overlay: dict[tuple[int, int], tuple[str, str]] = {}
    top = h // 2 - 2
    for r, s in enumerate(rows):
        x0 = (w - num_w) // 2
        for i, ch in enumerate(s):
            overlay[(top + r, x0 + i)] = (ch, "bold " + (C["ink"] if frac is not None else C["muted"]))
    ux = (w - len(unit)) // 2
    for i, ch in enumerate(unit):
        overlay[(top + 3, ux + i)] = (ch, C["muted"])
    lines = []
    for r in range(h):
        t = Text(no_wrap=True)
        for c in range(w):
            if (r, c) in overlay:
                ch, st = overlay[(r, c)]
                t.append(ch, style=st)
            elif filled[r][c]:
                t.append(chr(0x2800 + (filled[r][c] | track[r][c])), style=color)
            elif track[r][c]:
                t.append(chr(0x2800 + track[r][c]), style=C["faint"])
            else:
                t.append(" ")
        lines.append(t)
    return lines


# ---------------------------------------------------------------- bars

def hbar(frac: float | None, width: int, color: str, marker: float | None = None, track: str = "░") -> Text:
    """⅛-resolution progress bar. `marker` (0-1) draws a │ for a goal or average."""
    t = Text(no_wrap=True)
    if frac is None:
        return T(("·" * width, C["faint"]))
    f = max(0.0, min(1.0, frac)) * width
    full, part = int(f), int((f - int(f)) * 8)
    m = None if marker is None else min(width - 1, int(max(0.0, min(1.0, marker)) * width))
    for i in range(width):
        if i == m and i >= full:
            t.append("│", style=C["ink2"])
        elif i < full:
            t.append("█", style=color)
        elif i == full and part:
            t.append(HBLOCKS[part], style=color)
        else:
            t.append(track, style=C["faint"])
    return t


def segbar(parts: Sequence[tuple[float, str, str]], width: int) -> Text:
    """Stacked horizontal bar: (minutes, glyph, color). Largest-remainder rounding keeps total width."""
    total = sum(p[0] for p in parts) or 1
    raw = [p[0] / total * width for p in parts]
    cells = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - cells[i], reverse=True)[:width - sum(cells)]:
        cells[i] += 1
    t = Text(no_wrap=True)
    for (_, g, col), n in zip(parts, cells):
        t.append(g * n, style=col)
    return t


def spark(values: Sequence[float | None], color: str | Callable[[float], str], lo=None, hi=None,
          highlight_last: bool = True) -> Text:
    present = [v for v in values if v is not None]
    t = Text(no_wrap=True)
    if not present:
        return T(("·" * len(values), C["faint"]))
    lo = min(present) if lo is None else lo
    hi = max(present) if hi is None else hi
    for i, v in enumerate(values):
        if v is None:
            t.append("·", style=C["muted"])
            continue
        idx = 0 if hi == lo else int((v - lo) / (hi - lo) * 7.999)
        col = color(v) if callable(color) else color
        last = highlight_last and i == len(values) - 1
        t.append(SPARK[max(0, min(7, idx))], style=("bold " + C["ink"]) if last else col)
    return t


def columns(values: Sequence[float | None], height: int, col_w: int, gap: int,
            color: str | Callable[[float], str], top: float | None = None, goal: float | None = None,
            label_w: int = 6, fmt: Callable[[float], str] = lambda v: "{:.0f}".format(v),
            value_fmt: Callable[[float], str] | None = None,
            today_fmt: Callable[[float], str] | None = None) -> list[Text]:
    """Vertical ⅛-block columns with an optional dotted goal line and '·' for missing days.

    Each bar carries its value just above its top when every label fits in its slot; when they'd
    crowd, only today's (last) bar is labeled, on a row of its own so it never covers a bar.
    The left axis shows the max, goal and min."""
    present = [v for v in values if v is not None]
    top = top or max(present + ([goal] if goal else []) + [1e-9]) * 1.05
    goal_row = None if goal is None else min(height - 1, int(goal / top * height))
    n = len(values)
    slot = col_w + gap
    plot_w = n * col_w + max(0, n - 1) * gap
    rows = height + 1                              # one extra row on top for labels
    grid = [[(" ", "")] * plot_w for _ in range(rows)]
    bar_top: dict[int, int] = {}
    for i, v in enumerate(values):
        x0 = i * slot
        last = i == n - 1
        for row in range(height):
            if i and goal_row == row:
                for g in range(gap):
                    grid[row][x0 - gap + g] = ("┈", C["muted"])
            if v is None:
                ch, st = ("·" if row == 0 else "┈" if row == goal_row else " "), C["muted"]
            else:
                level = v / top * height * 8 - row * 8
                st = ("bold " + C["ink"]) if last else (color(v) if callable(color) else color)
                if level >= 8:
                    ch = "█"
                elif level >= 1:
                    ch = EIGHTHS[int(level)]
                elif row == 0 and v > 0:
                    ch = "▁"
                elif row == goal_row:
                    ch, st = "┈", C["muted"]
                else:
                    ch = " "
                if ch not in (" ", "┈"):
                    bar_top[i] = row
            for c in range(col_w):
                grid[row][x0 + c] = (ch, st)

    vf = value_fmt or fmt
    labels = {i: vf(v) for i, v in enumerate(values) if v is not None}
    fits_all = bool(labels) and max(len(t) for t in labels.values()) <= max(col_w, slot - 1)

    def put(row: int, start: int, text: str, style: str) -> None:
        start = max(0, min(start, plot_w - len(text)))
        for j, ch in enumerate(text[:plot_w]):
            grid[row][start + j] = (ch, style)

    def free(row: int, start: int, length: int) -> bool:
        start = max(0, min(start, plot_w - length))
        return all(grid[row][start + j][0] in (" ", "┈") for j in range(min(length, plot_w)))

    if fits_all:
        for i, text in labels.items():
            if i == n - 1:
                continue
            row = min(rows - 1, bar_top.get(i, -1) + 1)
            put(row, i * slot + (col_w - len(text)) // 2, text, C["ink2"])
    if n - 1 in labels:
        x0 = (n - 1) * slot
        exact = today_fmt(values[-1]) if today_fmt else labels[n - 1]
        row = min(rows - 1, bar_top.get(n - 1, -1) + 1) if fits_all else rows - 1
        for text, r in ((exact, row), (exact, rows - 1), (labels[n - 1], row)):
            start = x0 + (col_w - len(text)) // 2 if fits_all else x0 + col_w - len(text)
            if free(r, start, len(text)):
                put(r, start, text, "bold " + C["ink"])
                break

    lines = []
    for row in range(rows - 1, -1, -1):
        if fits_all and row != goal_row:
            lab = ""                       # every bar carries its own value; axis numbers would only compete
        elif row == height - 1:
            lab = fmt(max(present)) if present else ""
        elif row == goal_row:
            lab = fmt(goal)
        elif row == 0:
            lab = fmt(min(present)) if present else ""
        else:
            lab = ""
        axis = "┤" if lab else ("│" if row < height else " ")
        t = T(("{:>{w}} ".format(lab, w=label_w - 1), C["muted"]), (axis, C["rule"]))
        for ch, st in grid[row]:
            t.append(ch, style=st)
        lines.append(t)
    return lines


# ---------------------------------------------------------------- braille line chart

def braille_line(values: Sequence[float | None], w: int, h: int, color: str,
                 band: tuple[float, float] | None = None, lo: float | None = None, hi: float | None = None,
                 zone_color: Callable[[float], str] | None = None, xs: Sequence[float] | None = None) -> list[Text]:
    """Braille polyline (2×4 dots per cell). Gaps in the data break the line. `band` draws a dotted
    baseline range; the last point is drawn in bold ink. `xs` (0-1) places points freely."""
    pts = [v for v in values if v is not None]
    if not pts:
        return [T(("·" * w, C["faint"]))] + [Text() for _ in range(h - 1)]
    lo = min(pts + ([band[0]] if band else [])) if lo is None else lo
    hi = max(pts + ([band[1]] if band else [])) if hi is None else hi
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    dw, dh = w * 2, h * 4
    line = [[0] * w for _ in range(h)]
    cell_val: dict[tuple[int, int], float] = {}
    bandg = [[0] * w for _ in range(h)]
    last_cell = None

    def ydot(v):
        return int(round((hi - v) / (hi - lo) * (dh - 1)))

    def put(x, y, grid, v=None):
        if 0 <= x < dw and 0 <= y < dh:
            grid[y // 4][x // 2] |= _BRAILLE_BITS[y % 4][x % 2]
            if v is not None:
                key = (y // 4, x // 2)
                cell_val[key] = max(cell_val.get(key, v), v)

    n = len(values)
    coords = []
    for i, v in enumerate(values):
        if v is None:
            coords.append(None)
            continue
        fx = xs[i] if xs is not None else (i / (n - 1) if n > 1 else 1.0)
        coords.append((int(round(fx * (dw - 1))), ydot(v), v))
    if band:
        for x in range(0, dw, 6):
            put(x, ydot(band[0]), bandg)
            put(x, ydot(band[1]), bandg)
    prev = None
    for c in coords:
        if c is None:
            prev = None
            continue
        x1, y1, v1 = c
        if prev is not None and (xs is None or x1 - prev[0] <= 4):
            x0, y0, v0 = prev
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            for s in range(steps + 1):
                put(int(round(x0 + (x1 - x0) * s / steps)), int(round(y0 + (y1 - y0) * s / steps)), line,
                    v0 + (v1 - v0) * s / steps)
        else:
            put(x1, y1, line, v1)
        prev = c
    for c in reversed(coords):
        if c is not None:
            last_cell = (c[1] // 4, c[0] // 2)
            break
    out = []
    for r in range(h):
        t = Text(no_wrap=True)
        for col in range(w):
            if line[r][col]:
                if (r, col) == last_cell:
                    st = "bold " + C["ink"]
                elif zone_color and (r, col) in cell_val:
                    st = zone_color(cell_val[(r, col)])
                else:
                    st = color
                t.append(chr(0x2800 + line[r][col]), style=st)
            elif bandg[r][col]:
                t.append(chr(0x2800 + bandg[r][col]), style=C["rule"])
            else:
                t.append(" ")
        out.append(t)
    return out


def trend_chart(values: Sequence[float | None], days: Sequence[str], w: int, h: int, color: str,
                fmt: Callable[[float], str], base: dict | None = None, title: str = "",
                zone_color=None) -> list[Text]:
    """Labeled Braille trend: max/min on the left, dotted ±1 SD baseline band, '·' under missing days,
    first date and 'today' underneath, and the 28-day baseline spelled out."""
    lab_w = max(len(fmt(v)) for v in values if v is not None) + 1 if any(v is not None for v in values) else 4
    present = [v for v in values if v is not None]
    last = next((v for v in reversed(values) if v is not None), None)
    today_lab = "" if last is None else fmt(last) + (" today" if values[-1] is not None else " last")
    ann_w = len(today_lab) + 3 if today_lab else 0
    plot_w = max(8, w - lab_w - 1 - ann_w)
    band = (base["mean"], base["mean"]) if base else None    # one faint dotted line at the 28-day average
    rows = braille_line(values, plot_w, h, color, band=band, zone_color=zone_color)
    ann_row = None
    if last is not None:                     # same scaling as braille_line, to find the row of the last point
        lo = min(present + ([band[0]] if band else []))
        hi = max(present + ([band[1]] if band else []))
        if hi - lo < 1e-9:
            hi, lo = hi + 1, lo - 1
        ann_row = int(round((hi - last) / (hi - lo) * (h * 4 - 1))) // 4
    out = []
    if title:
        out.append(fit(T((title, C["ink2"])), w))
    for i, r in enumerate(rows):
        lab = fmt(max(present)) if i == 0 and present else fmt(min(present)) if i == h - 1 and present else ""
        line = T(("{:>{w}}".format(lab, w=lab_w), C["muted"]), ("│" if lab else " ", C["rule"]), r)
        if i == ann_row:
            line.append(" ◂ ", style=C["muted"])
            v_txt, _, rest = today_lab.partition(" ")
            line.append(v_txt, style="bold " + C["ink"])
            line.append(" " + rest, style=C["muted"])
        out.append(fit(line, w))
    axis = Text(no_wrap=True)
    n = len(values)
    marks = [" "] * plot_w
    for i, v in enumerate(values):
        if v is None:
            marks[int(round(i / max(1, n - 1) * (plot_w - 1)))] = "·"
    axis.append(" " * (lab_w + 1))
    axis.append("".join(marks), style=C["muted"])
    out.append(fit(axis, w))
    first = _short_date(days[0]) if days else ""
    tail = "today"
    note = ""
    if base:
        note = "⠂⠂ 28d avg {} (±{})".format(fmt(base["mean"]).strip(), fmt(base["sd"]).strip())
    mid_room = plot_w - len(first) - len(tail) - 2
    mid = note if len(note) <= mid_room else ""
    pad = plot_w - len(first) - len(tail) - len(mid)
    left = pad // 2
    out.append(fit(T(" " * (lab_w + 1), (first, C["muted"]), " " * left, (mid, C["muted"]),
                     " " * (pad - left), (tail, "bold " + C["ink2"])), w))
    return out


def _short_date(iso: str) -> str:
    return "{}/{}".format(int(iso[5:7]), int(iso[8:10]))


# ---------------------------------------------------------------- sleep charts

def hypnogram(timeline: Sequence[dict], total_min: float, w: int, start_label: str, end_label: str) -> list[Text]:
    """Four lanes (Awake, REM, Light, Deep); each column shows the stage that covered most of its slice."""
    lanes = [("awake", "Awake"), ("rem", "REM"), ("light", "Light"), ("deep", "Deep")]
    lab_w = 6
    plot_w = max(10, w - lab_w)
    slot = total_min / plot_w if total_min else 1
    dominant: list[str | None] = []
    for i in range(plot_w):
        a, b = i * slot, (i + 1) * slot
        cover: dict[str, float] = {}
        for seg in timeline:
            ov = min(b, seg["end_min"]) - max(a, seg["start_min"])
            if ov > 0:
                cover[seg["stage"]] = cover.get(seg["stage"], 0) + ov
        dominant.append(max(cover, key=cover.get) if cover else None)
    out = []
    for key, name in lanes:
        t = T(("{:<{w}}".format(name, w=lab_w), C["muted"]))
        for st in dominant:
            if st == key:
                t.append(STAGE_GLYPH[key], style=C[key])
            else:
                t.append("·" if st is None else " ", style=C["faint"])
        out.append(fit(t, w))
    pad = plot_w - len(start_label) - len(end_label)
    out.append(fit(T(" " * lab_w, (start_label, C["muted"]), " " * max(1, pad), (end_label, C["muted"])), w))
    return out


def timing_chart(nights: Sequence[dict], w: int, marks: Sequence[tuple[float, str, str]],
                 window: tuple[float, float] = (21 * 60, 13 * 60)) -> list[Text]:
    """One row per night: a bar from fell-asleep to woke on a 9 pm → 1 pm axis, with vertical
    markers (minutes after midnight, glyph, color) for the bedtime goal, step target and wake anchor."""
    from data import local_dt  # noqa: avoid a hard dependency at import time
    lab_w, tail_w = 10, 7
    plot_w = max(16, w - lab_w - tail_w)
    span_min = (window[1] - window[0]) % (24 * 60)

    def col(minutes_after_midnight: float) -> int:
        rel = (minutes_after_midnight - window[0]) % (24 * 60)
        if rel > span_min:                      # outside the axis: snap to the nearer edge, never wrap
            rel = 0 if rel > (span_min + 24 * 60) / 2 else span_min
        return int(round(rel / span_min * (plot_w - 1)))

    mcols = {col(m): (g, c) for m, g, c in marks}
    out = []
    for n in nights:
        from datetime import date as _d
        dd = _d.fromisoformat(n["date"])
        label = T(("{:<{w}}".format(dd.strftime("%a %-m/%-d"), w=lab_w), C["muted"]))
        cells = [(" ", "")] * plot_w
        for c, (g, colr) in mcols.items():
            cells[c] = (g, colr)
        if n.get("start"):
            s, e = local_dt(n["start"]), local_dt(n["end"])
            a, b = col(s.hour * 60 + s.minute), col(e.hour * 60 + e.minute)
            for i in range(a, max(a, b) + 1):
                cells[i] = ("█" if cells[i][0] == " " else "▓", C["sleep"])
            tail = "{:>{w}}".format(_hm(n.get("asleep")), w=tail_w)
            hrs = n.get("asleep") or 0
            tail_style = "bold " + (C["good"] if hrs >= 420 else C["watch"] if hrs >= 300 else C["flag"])  # 7h+ / 5–7h / <5h
        else:
            cells = [("·" if i % 4 == 0 else " ", C["faint"]) for i in range(plot_w)]
            for c, (g, colr) in mcols.items():
                cells[c] = (g, colr)
            tail = "{:>{w}}".format("—", w=tail_w)
            tail_style = C["muted"]
        t = label
        for g, colr in cells:
            t.append(g, style=colr)
        t.append(tail, style=tail_style)
        out.append(fit(t, w))
    axis = [" "] * plot_w
    for m, lab in ((21 * 60, "9p"), (0, "12a"), (3 * 60, "3a"), (6 * 60, "6a"), (9 * 60, "9a"), (12 * 60, "12p")):
        c = col(m)
        for i, ch in enumerate(lab):
            if c + i < plot_w:
                axis[c + i] = ch
    out.append(fit(T(" " * lab_w, ("".join(axis), C["muted"])), w))
    return out


def _hm(minutes) -> str:
    if minutes is None:
        return "—"
    h, m = divmod(int(round(minutes)), 60)
    return "{}h{:02d}".format(h, m) if h else "{}m".format(m)


# ---------------------------------------------------------------- strips & heatmaps

def heat_row(values: Sequence[float | None], cell_w: int, scale_max: float) -> Text:
    t = Text(no_wrap=True)
    for v in values:
        if v is None:
            t.append("·".center(cell_w), style=C["faint"])
            continue
        f = 0 if scale_max <= 0 else min(1.0, v / scale_max)
        g = " ▒▓█"[max(1, min(3, int(math.ceil(f * 3))))] if v > 0 else "░"
        t.append(g * cell_w, style=ramp(C["heat"], max(f, 0.35)) if v > 0 else C["faint"])
    return t


def recovery_dots(scores: Sequence[int | None]) -> Text:
    """● green, ◐ yellow, ○ red: shape carries the band too."""
    t = Text(no_wrap=True)
    for i, s in enumerate(scores):
        if s is None:
            t.append("·", style=C["muted"])
            continue
        band = "green" if s >= 67 else "yellow" if s >= 34 else "red"
        g = {"green": "●", "yellow": "◐", "red": "○"}[band]
        t.append(g, style=("bold " if i == len(scores) - 1 else "") + C["recovery"][band])
    return t


def chip(etype: str) -> Text:
    letter = CHIP_LETTER.get(etype, "·" if not etype else etype[0])
    col = C["chip"].get(etype, C["sleep"])
    return T((letter, "bold #111111 on " + col))
