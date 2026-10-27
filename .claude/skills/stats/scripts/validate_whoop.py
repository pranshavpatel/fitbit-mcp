#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""How close is fitdash's Recovery formula to WHOOP's, on a WHOOP user's own inputs?

    uv run --script validate_whoop.py ~/.fitbit-mcp/whoop/<export>/physiological_cycles.csv

Takes a WHOOP data export (Account -> Data export) and, for every cycle with 7+ earlier cycles,
feeds WHOOP's own overnight HRV, resting HR, sleep performance and respiratory rate into fitdash's
`scores.recovery` with the same 28-day baselines the dashboard uses. Comparing against WHOOP's
Recovery on identical inputs isolates the formula from sensor differences (Fitbit vs WHOOP strap).

LOGISTIC_X0 (the curve's centre) was fitted to this kind of data, so in-sample numbers flatter it.
The script also refits the centre leaving each day out in turn and scores that day with it
(leave-one-out), which is the honest out-of-sample estimate. Prints aggregates only, never a day.
"""
from __future__ import annotations

import argparse
import math
import csv
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scores as S  # noqa: E402

BASELINE_DAYS = 28   # same window as the dashboard

COLS = {"rec": "Recovery score %", "rhr": "Resting heart rate (bpm)", "hrv": "Heart rate variability (ms)",
        "sleep": "Sleep performance %", "resp": "Respiratory rate (rpm)", "start": "Cycle start time"}


def num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        rows = [{k: (r.get(c) if k == "start" else num(r.get(c))) for k, c in COLS.items()} for r in csv.DictReader(f)]
    return sorted((r for r in rows if r["start"]), key=lambda r: r["start"])


def raw_x(rows: list[dict], i: int) -> float | None:
    """fitdash's pre-logistic recovery total for cycle i, or None without enough baseline."""
    win = rows[max(0, i - BASELINE_DAYS):i]
    r = rows[i]
    res = S.recovery(r["hrv"], S.baseline(w["hrv"] for w in win), r["rhr"], S.baseline(w["rhr"] for w in win),
                     r["sleep"], r["resp"], S.baseline(w["resp"] for w in win))
    return res.x


def score(x: float, x0: float) -> float:
    return 100.0 / (1.0 + math.exp(-S.LOGISTIC_K * (x - x0)))


def fit_x0(pairs: list[tuple[float, float]]) -> float:
    grid = [i / 100 for i in range(-300, 301)]
    return min(grid, key=lambda c: sum(abs(score(x, c) - w) for x, w in pairs))


def band(v: float) -> str:
    return S.recovery_band(int(round(v)))


def ranks(v: list[float]) -> list[float]:
    s_ = sorted(v)
    return [(s_.index(a) + len(s_) - s_[::-1].index(a) - 1) / 2 for a in v]     # ties share the mean rank


def report(name: str, pred: list[float], whoop: list[float]) -> None:
    err = [p - w for p, w in zip(pred, whoop)]
    agree = sum(band(p) == band(w) for p, w in zip(pred, whoop)) / len(pred)
    rank = st.correlation(ranks(pred), ranks(whoop))
    print("  {:<34} MAE {:5.1f}   bias {:+5.1f}   r {:.2f}   Spearman {:.2f}   bands {:3.0f} %".format(
        name, st.fmean(abs(e) for e in err), st.fmean(err), st.correlation(pred, whoop), rank, 100 * agree))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv", type=Path)
    rows = load(ap.parse_args().csv)
    pairs = [(x, rows[i]["rec"]) for i in range(len(rows))
             if rows[i]["rec"] is not None and (x := raw_x(rows, i)) is not None]
    if len(pairs) < 5:
        sys.exit("need at least 5 scored cycles (7+ earlier cycles each); got {}".format(len(pairs)))
    xs, whoop = [x for x, _ in pairs], [w for _, w in pairs]
    print("{} cycles, {} with a 7+ day baseline   ({})".format(len(rows), len(pairs), S.FORMULA_VERSION))
    report("uncalibrated curve (centre 0)", [score(x, 0.0) for x in xs], whoop)
    report("shipped curve (centre {:+.2f})".format(S.LOGISTIC_X0), [score(x, S.LOGISTIC_X0) for x in xs], whoop)
    loo = [score(x, fit_x0(pairs[:i] + pairs[i + 1:])) for i, x in enumerate(xs)]
    report("leave-one-out refit centre", loo, whoop)
    mean_only = [st.fmean(whoop[:i] + whoop[i + 1:]) for i in range(len(whoop))]
    err = [abs(p - w) for p, w in zip(mean_only, whoop)]
    print("  {:<34} MAE {:5.1f}".format("baseline: predict the mean", st.fmean(err)))


if __name__ == "__main__":
    main()
