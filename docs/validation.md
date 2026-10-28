# Validation: how far can you trust the numbers?

fitdash's scores imitate WHOOP's scales, but they are its own formulas (see [scores.md](scores.md)).
This page records how they were checked: against WHOOP itself where that is possible, and against
the raw data where it isn't. It is honest about the sample: **one WHOOP user and one Fitbit user**.

## 1. Recovery vs WHOOP, on identical inputs

**Question.** Given the same overnight HRV, resting HR, sleep performance and respiratory rate, does
fitdash's Recovery formula land where WHOOP's does?

**Method.** A WHOOP user's data export (35 daily cycles) holds both WHOOP's inputs and its Recovery
score. [`validate_whoop.py`](../.claude/skills/stats/scripts/validate_whoop.py) feeds those inputs
into `scores.recovery` with the dashboard's baselines (the previous 28 calendar days) and compares
the outputs. Using WHOOP's own inputs isolates the formula from sensor differences between a Fitbit
and a WHOOP strap. Of the 35 cycles, 8 have under 7 days of baseline and 2 lack WHOOP's HRV, resting
HR or score, which leaves 25 scored days.

```text
uv run --script .claude/skills/stats/scripts/validate_whoop.py <export>/physiological_cycles.csv
```

| Curve | Days | MAE (points) | Bias | Pearson r | Spearman | Same band (green / yellow / red) |
| --- | --- | --- | --- | --- | --- | --- |
| Uncalibrated (centre 0) | 25 | 21.4 | −21.4 | 0.97 | 0.95 | 32 % |
| Shipped (centre −0.63), in sample | 25 | 7.0 | −2.6 | 0.95 | 0.95 | 84 % |
| **Walk-forward refit, out of sample** | 15 | **8.2** | −5.6 | 0.94 | 0.92 | **73 %** |
| Baseline: mean of earlier days | 15 | 14.6 | | | | |

**Reading it.**
- The structure was right from the start: the uncalibrated curve ranks days almost exactly as WHOOP
  does (r 0.97) but reads about 21 points low on every day. Only the logistic curve's centre was
  then fitted; the input weights (HRV 45 %, resting HR 30 %, sleep 25 %) were never tuned to WHOOP.
- The shipped centre was fitted on these same days, so 7.0 flatters it. The honest number is the
  walk-forward one: each of the last 15 days is scored with a centre fitted only on the days before
  it (at least 10), so nothing about that day reaches the fit. That gives **8.2**, a bit over half
  the error of guessing the average, with the band right about three times in four.

## 2. Strain: matching WHOOP's distribution

WHOOP's Day Strain can't be compared day by day, because the WHOOP user and the Fitbit user are
different people. What can be matched is the *spread*: an ordinary day, an easy day and a hard day
should land in similar places on the 0–21 scale.

All-day strain counts heart rate only at or above a fraction of heart-rate reserve. Without that
floor, sitting at 65–90 bpm all day added up to strain 12 on rest days. The floor was set to 30 %
so the two distributions line up:

| | Days | p25 | Median | p90 |
| --- | --- | --- | --- | --- |
| WHOOP user, WHOOP Day Strain | 33 | 7.4 | 9.6 | 15.0 |
| Fitbit user, fitdash (no floor) | 56 | | 14.3 | |
| Fitbit user, fitdash (30 % floor) | 56 | 7.4 | 10.2 | 16.3 |

This is a calibration of scale, not evidence that any single day matches what WHOOP would say.

## 3. Checking inputs against the raw data

A formula can be right and still be fed the wrong thing. Each Recovery input was traced to the raw
records for the day being scored.

- **HRV.** Fitbit's daily overnight HRV (78.6 ms on the day checked) agrees with the mean of that
  night's own RMSSD samples (84 ms; Fitbit weights toward deep sleep). Used as is.
- **Resting HR: replaced.** Fitbit's daily resting HR includes daytime rest and is smoothed across
  days. On the day checked it had risen exactly 1 bpm a day for four days (58 → 61), and because the
  smoothing makes its baseline very narrow (±1.5 bpm), that drift scored −1.9 SD and pulled Recovery
  to 50 %. Heart rate while actually asleep was 52 vs a usual 51 (−0.5 SD). Across 57 nights the
  two measures correlate only 0.63. WHOOP measures resting HR during sleep, so fitdash now does too
  (lowest 30-minute average during the main sleep); the same day scores 64 %. The 56-day average
  Recovery didn't move (61 %), so the change fixed individual days without shifting the scale.
- **Strain.** Active Zone Minutes miss everything below the fat-burn zone. On one long, active day,
  they gave strain 6.5 against 14.4 from all-day heart rate (before the floor in section 2), which
  is why fitdash syncs whole-day heart rate.

## 4. Engineering checks

| What | Before | After | How |
| --- | --- | --- | --- |
| Full dashboard render, 56 days of all-day HR (~2 M samples) | ~45 s | ~3 s warm | Covering partial index on samples, forced with `INDEXED BY` after the planner ignored it; per-day cache keyed on coverage, inputs and formula version |
| Hourly heart-rate sync | 20.6 MB | 0.29 MB | Incremental fetch from a stored high-water mark, with a 30-minute overlap |

Both test suites (MCP server and dashboard) run on Linux and macOS in CI. The dashboard tests use a
synthetic database and fail on any line wider than its panel at 80, 120 and 160 columns.

## Limits

- One WHOOP user over 25 scored days (15 out of sample), and one Fitbit user. Treat absolute numbers as estimates;
  rankings (which day was better) are the more reliable part.
- WHOOP's algorithm is proprietary. The comparison measures agreement, not that the formulas match.
- Not medical measurements.
