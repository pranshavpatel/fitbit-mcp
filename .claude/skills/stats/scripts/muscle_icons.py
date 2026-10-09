"""Pixel-art muscle icons drawn with half-block characters.

Each icon is 14 pixels wide × 12 tall. Two vertical pixels share one terminal cell (▀ ▄ █ with
foreground + background color), so an icon is 14 cells × 6 rows. Half-block pixels are about
square, so maps are drawn at true proportions.

Icons are *region maps*: every letter is a part of the body and `.` is empty. An icon lights one
region (the target muscle) and draws everything else as body. Upper-body muscles share the same
front or back torso, so the eye sees the same figure with a different part lit, like the
reference app. Leg and arm muscles get zoomed-in figures of their own.

    .  empty          d  seam (sternum, spine, muscle borders): always the dark body shade
    lowercase letter  a region; the icon's target region is the muscle, the rest is body
    uppercase letter  a seam *inside* that region (e.g. A between ab packs): drawn only when the
                      region is lit, so other icons don't get stray dark lines

Shading is automatic: a muscle pixel with no muscle above it is lit (highlight) and one with no
muscle below it is in shadow, which gives each muscle a rounded, 3-D look.

Symmetric figures are written as their left half (7 columns) and mirrored.
"""
from __future__ import annotations

from rich.style import Style
from rich.text import Text

W, H = 14, 12
BODY = "#5a5a5e"
SEAM = "#3d3d41"


def _mirror(left: list[str]) -> list[str]:
    return [row + row[::-1] for row in left]


# Front torso. h head/neck, t traps, s deltoid (a rounded cap that wraps down the arm), c pec,
# x upper arm, f forearm, o obliques, a abs (two packs split by a seam), w hips. The arms hang free
# of the tapering torso with a gap of empty space, which is what makes the silhouette read.
FRONT = _mirror([
    ".....hh",
    ".....hh",
    "......h",
    ".sstttt",
    "ssscccd",
    "ss.cccd",
    "xx.cccd",
    "xx.oaad",
    "xx.oAAd",
    "ff.oaad",
    "f...aad",
    "....www",
])

# Back torso. l latissimus: wide under the armpit, tapering to the waist (the V), spine seam.
BACK = _mirror([
    ".....hh",
    ".....hh",
    "......h",
    ".sstttt",
    "ssstttd",
    "ss.lltd",
    "xx.llld",
    "xx..lld",
    "xx..lld",
    "ff...ld",
    "f...bbd",
    "....www",
])

# Thighs from the front: q quadriceps (long teardrops), i inner thigh, k knee, n shin, w hips.
QUADS = _mirror([
    ".wwwwww",
    "wqqqqid",
    "qqqqqi.",
    "qqqqqi.",
    ".qqqqi.",
    ".qqqqi.",
    "..qqqi.",
    "..qqqq.",
    "..kkkk.",
    "..nnn..",
    "..nnn..",
    "..nnn..",
])

# Thighs from the back: g glutes (gray here), j hamstrings (two heads with a seam), k knee.
HAMSTRINGS = _mirror([
    ".gggggg",
    "ggggggd",
    "ggggggd",
    ".ggggg.",
    ".jjdjj.",
    ".jjdjj.",
    ".jjdjj.",
    "..jdj..",
    "..kkk..",
    "..nnn..",
    "..nnn..",
    "..nnn..",
])

# Hips from the back: g two big rounded lobes split by the cleft, lower back above, thighs below.
GLUTES = _mirror([
    "...bbbb",
    "...bbbb",
    "..ggggd",
    ".gggggd",
    "ggggggd",
    "ggggggd",
    ".ggggg.",
    "..ggg..",
    ".tttt..",
    ".tttt..",
    "..ttt..",
    "..ttt..",
])

# Lower legs from the back: k knee, v calf (two heads with a seam), then achilles and heel.
CALVES = _mirror([
    "..kkk..",
    "..kkk..",
    "..kkkk.",
    ".vvdvv.",
    "vvvdvv.",
    "vvvdvv.",
    ".vvdv..",
    "..vb...",
    "..bb...",
    "..bb...",
    "..bb...",
    ".bbb...",
])

# A flexed arm, shoulder at the bottom left: x biceps (top of the upper arm, the peak),
# r triceps (underside of the upper arm), f forearm rising to the fist, s shoulder.
ARM = [
    ".........fff..",
    "........fffff.",
    "........fffff.",
    ".........fff..",
    ".........fff..",
    "....xx...fff..",
    "..xxxxxx.fff..",
    ".xxxxxxxxfff..",
    "sxxxxxxxxxff..",
    "srrrrrrrrrf...",
    "ssrrrrrrr.....",
    "s.............",
]

ICONS: dict[str, tuple[list[str], str]] = {
    "chest": (FRONT, "c"),
    "shoulders": (FRONT, "s"),
    "abs": (FRONT, "a"),
    "lats": (BACK, "l"),
    "biceps": (ARM, "x"),
    "triceps": (ARM, "r"),
    "quads": (QUADS, "q"),
    "hamstrings": (HAMSTRINGS, "j"),
    "glutes": (GLUTES, "g"),
    "calves": (CALVES, "v"),
}


def validate() -> None:
    for name, (rows, region) in ICONS.items():
        assert len(rows) == H, (name, len(rows))
        for r in rows:
            assert len(r) == W, (name, r)
        flat = "".join(rows)
        assert region in flat, name
        assert any(c not in (".", "d", region) for c in flat), name    # there's body around the muscle


def _mix(hexcolor: str, target: int, amount: float) -> str:
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    f = lambda c: int(round(c + (target - c) * amount))  # noqa: E731
    return "#{:02x}{:02x}{:02x}".format(f(r), f(g), f(b))


def pixels(name: str) -> list[list[str]]:
    """Resolve an icon to tokens: '.' empty, 'b' body, 'd' seam, 'M' lit muscle, 'm' muscle, 'n' muscle shadow."""
    rows, region = ICONS[name]
    out = []
    for y, row in enumerate(rows):
        line = []
        for x, ch in enumerate(row):
            if ch == ".":
                line.append(".")
            elif ch == "d":
                line.append("d")
            elif ch.isupper():
                line.append("d" if ch.lower() == region else "b")
            elif ch != region:
                line.append("b")
            else:
                above = y > 0 and rows[y - 1][x] == region
                below = y < H - 1 and rows[y + 1][x] == region
                line.append("M" if not above else "n" if not below else "m")
        out.append(line)
    return out


def palette(muscle_color: str) -> dict[str, str]:
    return {"b": BODY, "d": SEAM, "m": muscle_color,
            "M": _mix(muscle_color, 255, 0.35), "n": _mix(muscle_color, 0, 0.30)}


def render(name: str, muscle_color: str, color: bool = True) -> list[Text]:
    """H/2 rows of W cells.

    With color, each cell is two pixels: ▀ with fg = top and bg = bottom (or ▄ / █ / space).
    Without color, one glyph per cell: █ where any muscle pixel is, ░ where only body is, so the
    target muscle still stands out in plain text."""
    px = pixels(name)
    pal = palette(muscle_color)
    out = []
    for y in range(0, H, 2):
        t = Text(no_wrap=True)
        for x in range(W):
            top, bot = px[y][x], px[y + 1][x]
            if not color:
                pair = top + bot
                t.append("█" if any(c in pair for c in "mMn") else "░" if any(c in pair for c in "bd") else " ")
                continue
            if top == "." and bot == ".":
                t.append(" ")
            elif bot == ".":
                t.append("▀", style=Style(color=pal[top]))
            elif top == ".":
                t.append("▄", style=Style(color=pal[bot]))
            elif pal[top] == pal[bot]:
                t.append("█", style=Style(color=pal[top]))
            else:
                t.append("▀", style=Style(color=pal[top], bgcolor=pal[bot]))
        out.append(t)
    return out


validate()
