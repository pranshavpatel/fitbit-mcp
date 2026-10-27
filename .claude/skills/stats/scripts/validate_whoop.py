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

LOGISTIC_X0 (the curve's centre) was fitted on this export, so in-sample numbers flatter it. The
script also walks forward: each later day is scored with a centre fitted only on the days before
it, the honest out-of-sample estimate. Prints aggregates only, never a day.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics as st
import sys
from datetime import date, timedelta
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
    """fitdash's pre-logistic recovery total for cycle i, or None without enough baseline. The
    baseline is the cycles of the previous 28 calendar days, like the dashboard's."""
    day = date.fromisoformat(rows[i]["start"][:10])
    lo, hi = (day - timedelta(days=BASELINE_DAYS)).isoformat(), (day - timedelta(days=1)).isoformat()
    win = [w for w in rows[:i] if lo <= w["start"][:10] <= hi]
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
    ap.add_argument("--min-train", type=int, default=10, help="earlier days needed before a day is scored out of sample")
    args = ap.parse_args()
    rows = load(args.csv)
    xs_all = [raw_x(rows, i) for i in range(len(rows))]
    no_base = sum(x is None and all(rows[i][k] is not None for k in ("hrv", "rhr")) for i, x in enumerate(xs_all))
    pairs = [(x, rows[i]["rec"]) for i, x in enumerate(xs_all) if x is not None and rows[i]["rec"] is not None]
    if len(pairs) < args.min_train + 5:
        sys.exit("need at least {} scored cycles; got {}".format(args.min_train + 5, len(pairs)))
    xs, whoop = [x for x, _ in pairs], [w for _, w in pairs]
    print("{} cycles: {} without 7 days of baseline, {} missing WHOOP's HRV, resting HR or score, "
          "{} scored   ({})".format(len(rows), no_base, len(rows) - no_base - len(pairs), len(pairs), S.FORMULA_VERSION))
    print("\nAll {} scored days (the shipped centre was fitted on these, so it is in-sample):".format(len(pairs)))
    report("uncalibrated curve (centre 0)", [score(x, 0.0) for x in xs], whoop)
    report("shipped curve (centre {:+.2f})".format(S.LOGISTIC_X0), [score(x, S.LOGISTIC_X0) for x in xs], whoop)

    # Walk forward: each day is scored with a centre fitted only on the days before it, and its own
    # inputs are in no earlier day's baseline, so nothing from the scored day reaches the fit.
    k = args.min_train
    test_x, test_w = xs[k:], whoop[k:]
    fwd = [score(x, fit_x0(pairs[:k + j])) for j, x in enumerate(test_x)]
    print("\nOut of sample: the last {} days, each scored with a centre fitted on earlier days only:".format(len(test_x)))
    report("walk-forward refit centre", fwd, test_w)
    report("shipped curve, same days", [score(x, S.LOGISTIC_X0) for x in test_x], test_w)
    mean_only = [st.fmean(whoop[:k + j]) for j in range(len(test_w))]
    print("  {:<34} MAE {:5.1f}".format("baseline: mean of earlier days", st.fmean(abs(p - w) for p, w in zip(mean_only, test_w))))


if __name__ == "__main__":
    main()
