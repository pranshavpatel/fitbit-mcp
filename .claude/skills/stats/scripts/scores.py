"""Score math for the stats dashboard. Pure functions: no I/O, no clock, no database.

These are transparent estimates calibrated on the user's own history. They borrow WHOOP's
scales (Recovery 0-100 %, Strain 0-21, Sleep Performance %) so they feel familiar, but they are
not WHOOP's proprietary algorithm. Every constant lives here so the footer's version string
identifies exactly what produced a number.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

FORMULA_VERSION = "stats-scores v1.1"

# ---------------------------------------------------------------- baselines


@dataclass(frozen=True)
class Baseline:
    mean: float
    sd: float
    n: int


def baseline(values: Iterable[float | None], min_n: int = 7) -> Baseline | None:
    """Mean and population SD of the readings that exist. Missing readings are skipped, never zero."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < min_n:
        return None
    return Baseline(statistics.fmean(vals), statistics.pstdev(vals), len(vals))


def zscore(value: float | None, base: Baseline | None, min_sd: float) -> float | None:
    """z against a baseline, with an SD floor so a very steady baseline can't blow small changes up."""
    if value is None or base is None:
        return None
    return (value - base.mean) / max(base.sd, min_sd)


def percentile(values: Sequence[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- recovery (0-100 %)

RECOVERY_WEIGHTS = {"hrv": 0.45, "rhr": 0.30, "sleep": 0.25}
RECOVERY_WEIGHTS_NO_SLEEP = {"hrv": 0.60, "rhr": 0.40}
LOGISTIC_K = 1.5          # steepness of the 0-100 mapping
LOGISTIC_X0 = -0.17       # x = 0 (every input exactly at baseline) maps to ~56 %
SLEEP_Z_CENTER, SLEEP_Z_SCALE = 85.0, 15.0   # Sleep Performance 85 % is neutral, 100 % is +1
PENALTY_SLOPE, PENALTY_FREE_Z, PENALTY_CAP = 0.35, 1.5, 1.0
HRV_MIN_SD_FRACTION = 0.05   # HRV SD floor: 5 % of its mean
RHR_MIN_SD = 1.0             # bpm
RESP_MIN_SD = 0.3            # breaths/min


def logistic(x: float) -> float:
    return 100.0 / (1.0 + math.exp(-LOGISTIC_K * (x - LOGISTIC_X0)))


def recovery_band(score: float | None) -> str | None:
    if score is None:
        return None
    return "green" if score >= 67 else "yellow" if score >= 34 else "red"


@dataclass
class Contribution:
    label: str
    z: float | None
    weighted: float
    points: float        # how many score points this input moves the result, holding the others


@dataclass
class RecoveryResult:
    score: int | None
    band: str | None
    reason: str | None = None
    x: float | None = None
    contributions: list[Contribution] = field(default_factory=list)


def _penalty(z: float | None) -> float:
    if z is None:
        return 0.0
    return min(PENALTY_CAP, PENALTY_SLOPE * max(0.0, abs(z) - PENALTY_FREE_Z))


def recovery(hrv: float | None, hrv_base: Baseline | None, rhr: float | None, rhr_base: Baseline | None,
             sleep_performance: float | None = None, resp: float | None = None, resp_base: Baseline | None = None,
             skin_dev_c: float | None = None, skin_sd_c: float | None = None) -> RecoveryResult:
    """Recovery from overnight HRV and resting HR z-scores (RHR inverted), Sleep Performance, and
    penalties when respiratory rate or skin temperature sit well outside their usual range.

    x = Σ wᵢ·zᵢ − penalties, score = logistic(x). HRV and RHR are required: without them there's
    nothing physiological to recover from, so the result is "—" with a reason, never a guess.
    """
    if hrv is None:
        return RecoveryResult(None, None, "no overnight HRV")
    if rhr is None:
        return RecoveryResult(None, None, "no resting HR")
    if hrv_base is None or rhr_base is None:
        return RecoveryResult(None, None, "under 7 days of baseline")

    z_hrv = clamp(zscore(hrv, hrv_base, hrv_base.mean * HRV_MIN_SD_FRACTION), -3, 3)
    z_rhr = clamp(-zscore(rhr, rhr_base, RHR_MIN_SD), -3, 3)
    terms: list[tuple[str, float | None, float]] = []
    if sleep_performance is not None:
        z_sleep = clamp((sleep_performance - SLEEP_Z_CENTER) / SLEEP_Z_SCALE, -2.5, 1.0)
        w = RECOVERY_WEIGHTS
        terms += [("HRV", z_hrv, w["hrv"] * z_hrv), ("Resting HR", z_rhr, w["rhr"] * z_rhr),
                  ("Sleep", z_sleep, w["sleep"] * z_sleep)]
    else:
        w = RECOVERY_WEIGHTS_NO_SLEEP
        terms += [("HRV", z_hrv, w["hrv"] * z_hrv), ("Resting HR", z_rhr, w["rhr"] * z_rhr)]

    z_resp = zscore(resp, resp_base, RESP_MIN_SD)
    if z_resp is not None:
        terms.append(("Resp. rate", z_resp, -_penalty(z_resp)))
    if skin_dev_c is not None and skin_sd_c:
        z_skin = skin_dev_c / skin_sd_c
        terms.append(("Skin temp", z_skin, -_penalty(z_skin)))

    x = sum(t[2] for t in terms)
    score = logistic(x)
    contributions = [Contribution(label, z, weighted, round(score - logistic(x - weighted), 1))
                     for label, z, weighted in terms]
    rounded = int(round(score))
    return RecoveryResult(rounded, recovery_band(rounded), None, round(x, 3), contributions)


# ---------------------------------------------------------------- strain (0-21)

TRIMP_A, TRIMP_B = 0.64, 1.92        # Banister (1991) coefficients
# Mid-zone heart-rate reserve used when only zone minutes exist. Fitbit's Active Zone Minutes zones
# start at roughly 40 / 60 / 85 % HRR (fat burn / cardio / peak); exercise summaries use four zones.
ZONE_HRR = {"FAT_BURN": 0.50, "CARDIO": 0.72, "PEAK": 0.90,
            "light": 0.40, "moderate": 0.55, "vigorous": 0.72, "peak": 0.90}
STRAIN_TAU = 25.0        # TRIMP where the curve starts bending
STRAIN_CAP_TRIMP = 500.0  # TRIMP that would reach 21
MAX_SAMPLE_GAP_S = 60.0   # a gap longer than this between HR samples isn't credited


def trimp_minutes(minutes: float, hrr: float) -> float:
    hrr = clamp(hrr, 0.0, 1.0)
    return max(0.0, minutes) * hrr * TRIMP_A * math.exp(TRIMP_B * hrr)


AZM_TO_ZONE = {"FAT_BURN": "moderate", "CARDIO": "vigorous", "PEAK": "peak"}


def zone_hrr_from_thresholds(thresholds: dict[str, tuple[float, float]], hr_rest: float | None,
                             hr_max: float | None) -> dict[str, float]:
    """Calibrate mid-zone heart-rate reserve from the user's own zone limits (bpm).

    `thresholds` maps light/moderate/vigorous/peak to (min_bpm, max_bpm). The light zone starts at
    resting HR, so its "exercise" midpoint is the middle of its upper half. Peak is capped at HRmax.
    Falls back to ZONE_HRR for anything that can't be calibrated."""
    out = dict(ZONE_HRR)
    if hr_rest is None or hr_max is None or hr_max <= hr_rest:
        return out

    def hrr(bpm: float) -> float:
        return clamp((bpm - hr_rest) / (hr_max - hr_rest), 0.0, 1.0)

    for zone, (lo, hi) in thresholds.items():
        if zone not in ("light", "moderate", "vigorous", "peak") or lo is None or hi is None:
            continue
        hi = min(hi, hr_max)
        if zone == "light":
            lo = (max(lo, hr_rest) + hi) / 2
        if hi > lo:
            out[zone] = round(hrr((lo + hi) / 2), 3)
    for azm, zone in AZM_TO_ZONE.items():
        out[azm] = out[zone]
    return out


def trimp_from_zones(zone_minutes: dict[str, float | None], zone_hrr: dict[str, float] | None = None) -> float | None:
    """Banister TRIMP from minutes per zone. Unknown zone names are ignored; all-missing → None."""
    hrr_map = zone_hrr or ZONE_HRR
    known = {z: m for z, m in zone_minutes.items() if z in hrr_map and m is not None}
    if not known:
        return None
    return sum(trimp_minutes(m, hrr_map[z]) for z, m in known.items())


def trimp_from_samples(samples: Sequence[tuple[float, float]], hr_rest: float | None,
                       hr_max: float | None) -> float | None:
    """Banister TRIMP from (seconds, bpm) samples using heart-rate reserve. Each sample is credited
    until the next one, capped at MAX_SAMPLE_GAP_S so dropouts aren't counted as effort."""
    if not samples or hr_rest is None or hr_max is None or hr_max <= hr_rest:
        return None
    pts = sorted(samples)
    total = 0.0
    for (t1, bpm), (t2, _) in zip(pts, pts[1:]):
        dt = min(max(t2 - t1, 0.0), MAX_SAMPLE_GAP_S) / 60.0
        total += trimp_minutes(dt, (bpm - hr_rest) / (hr_max - hr_rest))
    return total


def strain(trimp: float | None) -> float | None:
    """Logarithmic 0-21 scale: each extra point needs more load than the last."""
    if trimp is None:
        return None
    raw = 21.0 * math.log1p(max(trimp, 0.0) / STRAIN_TAU) / math.log1p(STRAIN_CAP_TRIMP / STRAIN_TAU)
    return round(min(21.0, raw), 1)


def strain_target(recovery_score: float | None) -> tuple[float, float] | None:
    """Suggested day-strain range for a recovery level."""
    if recovery_score is None:
        return None
    if recovery_score >= 67:
        return (14.0, 18.0)
    if recovery_score >= 34:
        return (10.0, 14.0)
    return (4.0, 10.0)


# ---------------------------------------------------------------- sleep

NEED_DEFAULT_MIN = 480.0
NEED_MIN, NEED_MAX = 420.0, 540.0       # personal baseline need is clamped to 7-9 h
REBOUND_AFTER_MIN = 360.0               # a night after one under 6 h is a rebound: left out of the baseline
DEBT_NIGHTS = 7                         # the debt balance looks back a week
DEBT_DECAY = 0.85                       # each night, older debt fades by 15 %
DEBT_REPAY_RATE = 1.0                   # an hour over need pays back an hour of debt
DEBT_ADJ_RATE, DEBT_ADJ_CAP = 0.5, 60.0
STRAIN_ADJ_FREE, STRAIN_ADJ_PER_POINT, STRAIN_ADJ_CAP = 10.0, 4.0, 45.0
NAP_CREDIT_CAP = 90.0


def baseline_sleep_need(nights: Sequence[float | None], fixed: float | None = None) -> tuple[float, bool]:
    """Personal need = 75th percentile of recent nights (what you sleep when nothing cuts it short),
    clamped to 7-9 h, leaving out *rebound* nights: a night that follows one under REBOUND_AFTER_MIN
    is the body repaying debt, not its normal need, and with a boom-bust schedule those nights would
    inflate the baseline. `nights` is oldest first; the first one only serves as the night before the
    second. `fixed` (minutes, from "sleep_need" in stats.json) overrides all of it.
    Returns (minutes, calibrated?)."""
    if fixed:
        return clamp(fixed, NEED_MIN, NEED_MAX), True
    nights = list(nights)
    vals = [a for prev, a in zip(nights, nights[1:])
            if a is not None and not (prev is not None and prev < REBOUND_AFTER_MIN)]
    if len(vals) < 7:                           # too few ordinary nights: fall back to all of them
        vals = [a for a in nights[1:] if a is not None]
    if len(vals) < 7:
        return NEED_DEFAULT_MIN, False
    return clamp(percentile(vals, 0.75), NEED_MIN, NEED_MAX), True


def sleep_debt(nights: Sequence[float | None], base_need: float) -> float:
    """Running sleep-debt balance over the last DEBT_NIGHTS nights, oldest first: each night adds its
    shortfall and a long night pays debt back (surplus × DEBT_REPAY_RATE), the balance never drops
    below zero, and older debt fades by DEBT_DECAY per night. Missing nights change nothing."""
    balance = 0.0
    for a in list(nights)[-DEBT_NIGHTS:]:
        balance *= DEBT_DECAY
        if a is None:
            continue
        diff = base_need - a
        balance = max(0.0, balance + (diff if diff > 0 else diff * DEBT_REPAY_RATE))
    return balance


@dataclass
class SleepNeed:
    total: float
    base: float
    strain_adj: float
    debt_adj: float
    nap_credit: float
    debt: float


def sleep_need(base_need: float, prev_day_strain: float | None, debt: float, nap_minutes: float) -> SleepNeed:
    strain_adj = 0.0 if prev_day_strain is None else clamp(
        (prev_day_strain - STRAIN_ADJ_FREE) * STRAIN_ADJ_PER_POINT, 0.0, STRAIN_ADJ_CAP)
    debt_adj = min(debt * DEBT_ADJ_RATE, DEBT_ADJ_CAP)
    nap_credit = min(max(nap_minutes, 0.0), NAP_CREDIT_CAP)
    total = max(base_need + strain_adj + debt_adj - nap_credit, 300.0)
    return SleepNeed(round(total), round(base_need), round(strain_adj), round(debt_adj), round(nap_credit), round(debt))


def sleep_performance(asleep: float | None, need: float | None) -> int | None:
    if asleep is None or not need:
        return None
    return int(round(min(100.0, 100.0 * asleep / need)))


def efficiency(asleep: float | None, in_bed: float | None) -> int | None:
    if asleep is None or not in_bed:
        return None
    return int(round(min(100.0, 100.0 * asleep / in_bed)))


def clock_offset(minutes_after_midnight: float) -> float:
    """Minutes since 6 pm, so 11 pm and 1 am land next to each other (300 and 420)."""
    return (minutes_after_midnight - 18 * 60) % (24 * 60)


def consistency(onsets: Sequence[float | None], wakes: Sequence[float | None]) -> int | None:
    """100 % = identical bed and wake times every night; an average SD of 2 h scores 0.
    Inputs are minutes after midnight; needs at least 3 nights."""
    pairs = [(clock_offset(o), clock_offset(w)) for o, w in zip(onsets, wakes) if o is not None and w is not None]
    if len(pairs) < 3:
        return None
    sd = (statistics.pstdev(p[0] for p in pairs) + statistics.pstdev(p[1] for p in pairs)) / 2
    return int(round(clamp(100.0 - sd / 1.2, 0.0, 100.0)))


# ---------------------------------------------------------------- training load

ACWR_SWEET = (0.8, 1.3)
ACWR_WARN = 1.5


@dataclass
class LoadRatio:
    ratio: float | None
    acute: float | None
    chronic: float | None
    zone: str
    reason: str | None = None


def acwr(daily_loads: Sequence[float | None]) -> LoadRatio:
    """Acute (last 7 days) ÷ chronic (last 28 days) mean daily load. `daily_loads` is chronological
    and ends today. Days without data are skipped rather than counted as rest."""
    last28 = [v for v in daily_loads[-28:] if v is not None]
    last7 = [v for v in daily_loads[-7:] if v is not None]
    if len(last7) < 4 or len(last28) < 14:
        return LoadRatio(None, None, None, "unknown", "needs 14 of 28 days with data")
    acute, chronic = statistics.fmean(last7), statistics.fmean(last28)
    if chronic <= 0:
        return LoadRatio(None, acute, chronic, "unknown", "no chronic load yet")
    r = acute / chronic
    zone = "low" if r < ACWR_SWEET[0] else "sweet spot" if r <= ACWR_SWEET[1] else "caution" if r <= ACWR_WARN else "high"
    return LoadRatio(round(r, 2), round(acute, 1), round(chronic, 1), zone)


def impact_limit(weekly_minutes: Sequence[float | None], ratio: float = 1.3) -> float | None:
    """Minutes this week can reach before it counts as a spike: ratio × the previous 4 weeks' mean."""
    prev = [p for p in weekly_minutes[-5:-1] if p is not None]
    return round(ratio * statistics.fmean(prev)) if prev else None


def asleep_by(wake_minutes: float, need_minutes: float) -> float:
    """Clock time (minutes after midnight) to fall asleep to get `need` before a fixed wake time."""
    return (wake_minutes - need_minutes) % (24 * 60)


def impact_spikes(weekly_minutes: Sequence[float | None], ratio: float = 1.3, floor: float = 60.0) -> list[bool]:
    """A week is a spike when it's over `ratio`× the mean of the previous 4 weeks with data and
    above `floor` minutes (so 10 → 20 minutes isn't flagged)."""
    out = []
    for i, v in enumerate(weekly_minutes):
        prev = [p for p in weekly_minutes[max(0, i - 4):i] if p is not None]
        out.append(bool(v is not None and prev and v >= floor and v > ratio * statistics.fmean(prev)))
    return out


# ---------------------------------------------------------------- stress (0-3)


def stress_level(hr_values: Sequence[float], hr_rest: float | None, hr_max: float | None,
                 min_samples: int = 20) -> float | None:
    """Daytime HR elevation above resting for one hour of non-exercise samples, on a 0-3 scale:
    0 at resting HR, 3 when the median sits 30 % of heart-rate reserve above rest."""
    if len(hr_values) < min_samples or hr_rest is None or hr_max is None or hr_max <= hr_rest:
        return None
    hrr = (statistics.median(hr_values) - hr_rest) / (hr_max - hr_rest)
    return round(clamp(hrr / 0.10, 0.0, 3.0), 1)


# ---------------------------------------------------------------- body

BULK_PACE = (0.25, 0.5)   # % of body weight per week
PACE_TOLERANCE = 0.05     # within 0.05 %/wk of an edge counts as "at the edge", not off pace


@dataclass
class WeightTrend:
    kg_per_week: float | None
    pct_per_week: float | None
    status: str
    reason: str | None = None


def weight_trend(points: Sequence[tuple[float, float]], min_span_days: float = 14.0) -> WeightTrend:
    """Least-squares slope through (day_number, kg). Needs 2+ weigh-ins at least two weeks apart."""
    pts = sorted(points)
    if len(pts) < 2:
        return WeightTrend(None, None, "unknown", "needs 2+ weigh-ins")
    span = pts[-1][0] - pts[0][0]
    if span < min_span_days:
        return WeightTrend(None, None, "unknown", "weigh-ins span under 2 weeks")
    mx = statistics.fmean(p[0] for p in pts)
    my = statistics.fmean(p[1] for p in pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
    kg_week = slope * 7
    pct = kg_week / pts[-1][1] * 100
    tol = PACE_TOLERANCE
    if pct < BULK_PACE[0] - tol:
        status = "below pace"
    elif pct < BULK_PACE[0]:
        status = "at the low edge"
    elif pct <= BULK_PACE[1]:
        status = "on pace"
    elif pct <= BULK_PACE[1] + tol:
        status = "at the high edge"
    else:
        status = "above pace"
    return WeightTrend(round(kg_week, 2), round(pct, 2), status)


# ---------------------------------------------------------------- workouts & streaks

GENERIC_TYPES = {None, "", "WORKOUT", "OTHER", "SPORT", "UNKNOWN", "EXERCISE_TYPE_UNSPECIFIED"}
METHOD_RANK = {"ACTIVELY_MEASURED": 3, "MANUAL": 2, "PASSIVELY_MEASURED": 1}


def dedupe_sessions(sessions: Sequence[dict]) -> list[dict]:
    """Drop sessions that overlap a better one by ≥ 50 % of the shorter session.
    Each session needs 'start' and 'end' (comparable, e.g. epoch seconds), and may carry 'type' and
    'method'. Preference: a specific type over a generic one, then the recording method (user-started
    > manual > auto-detected), then the longer session."""
    def rank(s):
        return (s.get("type") not in GENERIC_TYPES, METHOD_RANK.get(s.get("method"), 0), s["end"] - s["start"])

    kept: list[dict] = []
    for s in sorted(sessions, key=rank, reverse=True):
        dur = s["end"] - s["start"]
        clash = False
        for k in kept:
            overlap = min(s["end"], k["end"]) - max(s["start"], k["start"])
            shorter = min(dur, k["end"] - k["start"])
            if overlap > 0 and shorter > 0 and overlap >= 0.5 * shorter:
                clash = True
                break
        if not clash:
            kept.append(s)
    return sorted(kept, key=lambda s: s["start"])


def union_minutes(intervals: Iterable[tuple[float, float]]) -> float:
    """Total minutes covered by (start_s, end_s) intervals, overlaps counted once."""
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total / 60.0


@dataclass
class Streak:
    current: int
    best: int


def streak(flags: Sequence[bool | None], today_partial: bool = False) -> Streak:
    """Consecutive True values ending at the latest day (chronological input). A missing day (None)
    breaks a streak. If today is still in progress and not yet met, the streak counts from yesterday."""
    seq = list(flags)
    if today_partial and seq and not seq[-1]:
        seq = seq[:-1]
    current = 0
    for f in reversed(seq):
        if f:
            current += 1
        else:
            break
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return Streak(current, max(best, current))


# ---------------------------------------------------------------- training zones (5-zone, % of HRmax)

ZONE_PCT = (0.50, 0.60, 0.70, 0.80, 0.90)   # lower bound of zones 1-5 as a fraction of HRmax


def hr_zone(bpm: float, hr_max: float) -> int:
    """0 = below zone 1, else 1-5."""
    f = bpm / hr_max
    z = 0
    for i, lo in enumerate(ZONE_PCT, start=1):
        if f >= lo:
            z = i
    return z


def zone_minutes(samples: Sequence[tuple[float, float]], hr_max: float | None) -> dict[int, float] | None:
    """Minutes in each zone 1-5 from (seconds, bpm) samples. Time below zone 1 counts as zone 1,
    so the split adds up to the whole session. Gaps over MAX_SAMPLE_GAP_S aren't credited."""
    if not samples or not hr_max:
        return None
    pts = sorted(samples)
    out = {z: 0.0 for z in range(1, 6)}
    for (t1, bpm), (t2, _) in zip(pts, pts[1:]):
        dt = min(max(t2 - t1, 0.0), MAX_SAMPLE_GAP_S) / 60.0
        out[max(1, hr_zone(bpm, hr_max))] += dt
    return out if sum(out.values()) > 0 else None


def dominant_zone(minutes: dict[int, float] | None) -> int | None:
    if not minutes:
        return None
    return max(minutes, key=lambda z: (minutes[z], z))


# ---------------------------------------------------------------- muscle freshness (fatigue model)
#
# Each session adds fatigue to the muscles it worked: dose × share (1 for primary muscles, less for
# secondary ones). Fatigue halves every half-life, faster after a green recovery and slower after a
# red one. Freshness = 100 − remaining fatigue. Fitbit doesn't record sets or reps, so the dose comes
# from session duration and heart-rate strain; it is an estimate, and the panel says so.

MUSCLES = ("chest", "shoulders", "lats", "biceps", "triceps", "abs", "quads", "hamstrings", "glutes", "calves")
HALF_LIFE_H = {"chest": 48, "lats": 48, "quads": 48, "hamstrings": 48, "glutes": 48,
               "shoulders": 36, "biceps": 36, "triceps": 36, "abs": 36, "calves": 36}
SECONDARY_SHARE = 0.5
MINOR_SHARE = 0.25                  # e.g. rear delts on a pull day
DOSE_REF = 70.0                     # fatigue a 60-minute session at strain 8 leaves on its primary muscles
FRESH_WINDOW_H = 7 * 24             # sessions older than this no longer count
LEGS = ("quads", "hamstrings", "glutes", "calves")
IMPACT_SHARE = {"RUNNING": 0.6, "TREADMILL_RUNNING": 0.6, "TRAIL_RUN": 0.7, "SOCCER": 0.7,
                "PLYOMETRICS": 0.8, "JUMP_ROPE": 0.6}


def split_targets(day_name: str | None) -> dict[str, float]:
    """Muscle → share of the session's fatigue, read from the split day's name."""
    if not day_name:
        return {}
    n = day_name.lower()
    t: dict[str, float] = {}

    def add(muscle: str, share: float) -> None:
        t[muscle] = max(t.get(muscle, 0.0), share)

    if "push" in n or "chest" in n:
        add("chest", 1.0); add("shoulders", SECONDARY_SHARE); add("triceps", SECONDARY_SHARE)  # noqa: E702
    if "pull" in n or "back" in n or "lat" in n:
        add("lats", 1.0); add("biceps", SECONDARY_SHARE); add("shoulders", MINOR_SHARE)       # noqa: E702
    if "leg" in n:
        for mu in ("quads", "hamstrings", "glutes"):
            add(mu, 1.0)
        add("calves", SECONDARY_SHARE)
    if "abs" in n or "core" in n:
        add("abs", 1.0)
    if "arm" in n:
        add("biceps", 1.0); add("triceps", 1.0)                                                # noqa: E702
    if "shoulder" in n:
        add("shoulders", 1.0); add("triceps", SECONDARY_SHARE)                                  # noqa: E702
    return t


def activity_targets(workout_type: str | None) -> dict[str, float]:
    """Runs, soccer and plyometrics load the legs."""
    share = IMPACT_SHARE.get(workout_type or "")
    return {mu: share for mu in LEGS} if share else {}


def split_muscles(day_name: str | None) -> list[str]:
    """The muscles a split day works, primary first."""
    t = split_targets(day_name)
    return sorted(t, key=lambda mu: (-t[mu], MUSCLES.index(mu)))


def session_dose(minutes: float | None, strain: float | None) -> float:
    """Fatigue a session leaves on a primary muscle: grows with duration and strain, with
    diminishing returns, capped at 100."""
    if not minutes:
        return 0.0
    st = strain if strain is not None else 6.0
    return min(100.0, DOSE_REF * math.sqrt(max(minutes, 0.0) / 60.0) * math.sqrt(max(st, 1.0) / 8.0))


def recovery_rate(recovery_score: float | None) -> float:
    """How fast fatigue clears: 1.25× after a green recovery, 0.75× after a red one."""
    if recovery_score is None:
        return 1.0
    return 1.25 if recovery_score >= 67 else 1.0 if recovery_score >= 34 else 0.75


def fatigue_left(dose: float, hours: float, half_life_h: float, rate: float = 1.0) -> float:
    return dose * 0.5 ** (max(hours, 0.0) * rate / half_life_h)


def muscle_freshness(sessions: Sequence[dict], rate: float = 1.0) -> dict[str, int]:
    """sessions: [{"hours": since it ended, "dose": session_dose, "targets": {muscle: share}}].
    Returns freshness 0-100 for every muscle; untouched muscles (none in FRESH_WINDOW_H) are 100."""
    fatigue = {mu: 0.0 for mu in MUSCLES}
    for s in sessions:
        if s["hours"] < 0 or s["hours"] > FRESH_WINDOW_H:
            continue
        for mu, share in s["targets"].items():
            if mu in fatigue:
                fatigue[mu] += fatigue_left(s["dose"] * share, s["hours"], HALF_LIFE_H[mu], rate)
    return {mu: int(max(0, min(100, math.floor(100 - f + 0.5)))) for mu, f in fatigue.items()}


def freshness_color(pct: int | None) -> str:
    """State color: 100 neutral gray, 90-99 bright green, 70-89 green, 40-69 amber, under 40 red."""
    if pct is None:
        return "#8e8e93"
    if pct >= 100:
        return "#c7c7cc"
    if pct >= 90:
        return "#34c759"
    if pct >= 70:
        return "#30b350"
    if pct >= 40:
        return "#ffb340"
    return "#ff453a"


def freshness_dots(pct: int) -> int:
    """Filled dots out of 10: one per 10 %, rounded down, so anything under 100 % shows at most 9."""
    return max(0, min(10, int(pct) // 10))


# ---------------------------------------------------------------- sleep score (WHOOP-style)
#
# WHOOP's Sleep Performance (2025) has four components: hours vs. needed, sleep consistency over a
# rolling 4-day window, sleep efficiency, and sleep stress (time in a high-stress state overnight).
# WHOOP doesn't publish the weights; these are ours, with sufficiency weighted most because the
# legacy WHOOP score was sufficiency alone. Bands follow WHOOP's guidance that 90 %+ is the goal
# for optimal recovery: optimal ≥ 90, sufficient 70-89 (70 % of need is WHOOP's "get by" target),
# poor < 70.

SLEEP_SCORE_WEIGHTS = {"sufficiency": 0.50, "efficiency": 0.20, "consistency": 0.15, "stress": 0.15}
SLEEP_CONSISTENCY_NIGHTS = 4
STRESS_PERCENTILE = 0.20          # a 5-min HRV reading below your own 20th percentile = high stress
STRESS_MIN_SAMPLES = 6            # at least 30 minutes of overnight HRV readings
STRESS_MIN_BASELINE = 60          # and a usable baseline (≈ a few nights)


def sleep_stress_pct(night_hrv: Sequence[float], baseline_hrv: Sequence[float]) -> float | None:
    """Share of the night (in %) spent in high physiological stress: overnight 5-minute HRV
    readings below the 20th percentile of the previous 28 nights' readings."""
    night = [v for v in night_hrv if v is not None]
    base = [v for v in baseline_hrv if v is not None]
    if len(night) < STRESS_MIN_SAMPLES or len(base) < STRESS_MIN_BASELINE:
        return None
    cut = percentile(base, STRESS_PERCENTILE)
    return round(100.0 * sum(1 for v in night if v < cut) / len(night), 1)


def sleep_score_band(score: float | None) -> str | None:
    if score is None:
        return None
    return "optimal" if score >= 90 else "sufficient" if score >= 70 else "poor"


@dataclass
class SleepScore:
    score: int | None
    band: str | None
    components: dict = field(default_factory=dict)   # name -> {"value", "score", "weight"}
    reason: str | None = None


def sleep_score(sufficiency: float | None, efficiency: float | None, consistency: float | None,
                stress_pct: float | None) -> SleepScore:
    """0-100 weighted blend of the four component scores. Each component is already 0-100:
    sufficiency (asleep ÷ need, capped at 100), efficiency (asleep ÷ in bed), consistency (bed/wake
    regularity over 4 nights) and stress (100 − % of the night in high stress). A missing component
    is left out and the others are re-weighted; without sufficiency there is no score."""
    if sufficiency is None:
        return SleepScore(None, None, {}, "no night recorded")
    parts = {"sufficiency": min(100.0, sufficiency), "efficiency": efficiency, "consistency": consistency,
             "stress": None if stress_pct is None else max(0.0, 100.0 - stress_pct)}
    have = {k: v for k, v in parts.items() if v is not None}
    wsum = sum(SLEEP_SCORE_WEIGHTS[k] for k in have)
    score = sum(SLEEP_SCORE_WEIGHTS[k] * v for k, v in have.items()) / wsum
    comps = {k: {"score": None if v is None else round(v), "weight": round(SLEEP_SCORE_WEIGHTS[k] / wsum, 3) if v is not None else 0}
             for k, v in parts.items()}
    comps["stress"]["value"] = stress_pct
    s = int(round(score))
    return SleepScore(s, sleep_score_band(s), comps)


# ---------------------------------------------------------------- lifting: exercises, sets, overload
#
# Each exercise maps to primary muscles (a hard set counts 1 set for them) and secondary muscles
# (½ set), the usual convention for weekly-volume counting. Names are matched loosely (aliases,
# plurals, "db"/"bb" prefixes) so `fitdash lift` can stay quick to type.

EXERCISES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "bench press": (("chest",), ("triceps", "shoulders")),
    "incline bench press": (("chest",), ("shoulders", "triceps")),
    "dumbbell press": (("chest",), ("triceps", "shoulders")),
    "incline dumbbell press": (("chest",), ("shoulders", "triceps")),
    "chest press": (("chest",), ("triceps", "shoulders")),             # machine
    "single arm pushdown": (("triceps",), ()),
    "chest fly": (("chest",), ()),
    "dip": (("chest", "triceps"), ("shoulders",)),
    "push up": (("chest",), ("triceps", "shoulders")),
    "overhead press": (("shoulders",), ("triceps",)),
    "lateral raise": (("shoulders",), ()),
    "rear delt fly": (("shoulders",), ()),
    "face pull": (("shoulders",), ("lats",)),
    "row": (("lats",), ("biceps", "shoulders")),
    "pull up": (("lats",), ("biceps",)),
    "chin up": (("lats", "biceps"), ()),
    "lat pulldown": (("lats",), ("biceps",)),
    "deadlift": (("hamstrings", "glutes"), ("lats", "quads")),
    "romanian deadlift": (("hamstrings",), ("glutes",)),
    "squat": (("quads", "glutes"), ("hamstrings",)),
    "leg press": (("quads",), ("glutes",)),
    "lunge": (("quads", "glutes"), ("hamstrings",)),
    "leg extension": (("quads",), ()),
    "leg curl": (("hamstrings",), ()),
    "hip thrust": (("glutes",), ("hamstrings",)),
    "calf raise": (("calves",), ()),
    "curl": (("biceps",), ()),
    "hammer curl": (("biceps",), ()),
    "triceps extension": (("triceps",), ()),
    "triceps pushdown": (("triceps",), ()),
    "skull crusher": (("triceps",), ()),
    "crunch": (("abs",), ()),
    "leg raise": (("abs",), ()),
    "plank": (("abs",), ()),
}
EXERCISE_ALIASES = {
    "bench": "bench press", "incline": "incline bench press", "incline bench": "incline bench press",
    "db press": "dumbbell press", "dbp": "dumbbell press",
    "incline db press": "incline dumbbell press", "incline dbp": "incline dumbbell press",
    "incline db bench": "incline dumbbell press", "incline dumbbell bench press": "incline dumbbell press",
    "machine chest press": "chest press", "machine press": "chest press",
    "decline fly": "chest fly", "decline flye": "chest fly", "incline fly": "chest fly", "cable fly": "chest fly",
    "single hand pushdown": "single arm pushdown", "one arm pushdown": "single arm pushdown",
    "single arm tricep pushdown": "single arm pushdown", "single hand tricep pushdown": "single arm pushdown",
    "single arm triceps pushdown": "single arm pushdown", "single hand triceps pushdown": "single arm pushdown",
    "overhead tricep extension": "triceps extension", "overhead triceps extension": "triceps extension", "db bench": "dumbbell press", "dumbbell bench": "dumbbell press",
    "db bench press": "dumbbell press", "dumbbell bench press": "dumbbell press", "fly": "chest fly", "flye": "chest fly", "pec deck": "chest fly",
    "dips": "dip", "pushup": "push up", "pushups": "push up", "ohp": "overhead press",
    "shoulder press": "overhead press", "military press": "overhead press", "lateral": "lateral raise",
    "laterals": "lateral raise", "side raise": "lateral raise", "rear delt": "rear delt fly",
    "facepull": "face pull", "barbell row": "row", "cable row": "row", "seated row": "row", "bent over row": "row",
    "pullup": "pull up", "pullups": "pull up", "chinup": "chin up", "chinups": "chin up",
    "pulldown": "lat pulldown", "lat pull": "lat pulldown", "rdl": "romanian deadlift", "dl": "deadlift",
    "back squat": "squat", "front squat": "squat", "goblet squat": "squat", "lunges": "lunge",
    "split squat": "lunge", "bulgarian split squat": "lunge", "leg ext": "leg extension", "hamstring curl": "leg curl",
    "glute bridge": "hip thrust", "calf": "calf raise", "calves": "calf raise", "bicep curl": "curl",
    "biceps curl": "curl", "curls": "curl", "hammer": "hammer curl", "pushdown": "triceps pushdown",
    "tricep pushdown": "triceps pushdown", "tricep extension": "triceps extension", "overhead extension": "triceps extension",
    "skullcrusher": "skull crusher", "skullcrushers": "skull crusher", "crunches": "crunch", "leg raises": "leg raise",
}


def _known(n: str) -> str | None:
    """Exact name or alias, else its singular ("lateral raises", "flies", "presses")."""
    for plural, single in (("", ""), ("flies", "fly"), ("flyes", "flye"), ("presses", "press"), ("es", "e"), ("s", "")):
        if plural and not n.endswith(plural):
            continue
        cand = n[:-len(plural)] + single if plural else n
        if cand in EXERCISES or cand in EXERCISE_ALIASES:
            return EXERCISE_ALIASES.get(cand, cand)
    return None


def canonical_exercise(name: str) -> str | None:
    """Normalize a typed exercise name to an EXERCISES key, or None if it isn't known. Full names win
    ("db bench" is the dumbbell press, not the bench), then equipment prefixes are dropped
    ("cable lateral raises" → lateral raise)."""
    n = " ".join(name.lower().replace("-", " ").replace("_", " ").split())
    found = _known(n)
    if found:
        return found
    for prefix in ("db ", "bb ", "dumbbell ", "barbell ", "cable ", "machine ", "smith ", "seated ", "standing "):
        if n.startswith(prefix):
            found = _known(n[len(prefix):])
            if found:
                return found
    return None


def exercise_muscles(exercise: str) -> dict[str, float]:
    """Muscle → set credit per hard set (1 primary, ½ secondary)."""
    prim, sec = EXERCISES.get(exercise, ((), ()))
    out = {m: 0.5 for m in sec}
    out.update({m: 1.0 for m in prim})
    return out


def e1rm(weight: float | None, reps: int) -> float | None:
    """Estimated one-rep max (Epley): w × (1 + reps/30). None for bodyweight sets."""
    if not weight or reps <= 0:
        return None
    return weight * (1 + reps / 30.0) if reps > 1 else float(weight)


def weekly_sets(entries: Sequence[dict]) -> dict[str, float]:
    """Hard sets per muscle from lift-log entries ({"exercise", "sets"})."""
    out = {m: 0.0 for m in MUSCLES}
    for e in entries:
        for mu, credit in exercise_muscles(e["exercise"]).items():
            out[mu] += credit * e["sets"]
    return out


HYPERTROPHY_SETS = (10, 20)     # hard sets per muscle per week: the commonly cited hypertrophy range


def lift_dose(entries: Sequence[dict]) -> dict[str, float]:
    """Fatigue a logged session leaves per muscle, from real sets: 8 effective sets ≈ the dose of a
    typical 60-minute session at strain 8 (DOSE_REF), with diminishing returns, capped at 100."""
    sets = weekly_sets(entries)
    return {mu: min(100.0, DOSE_REF * math.sqrt(s / 8.0)) for mu, s in sets.items() if s > 0}


# ---------------------------------------------------------------- insights: what moves your recovery

@dataclass
class Effect:
    label: str
    diff: float            # mean(outcome | yes) − mean(outcome | no)
    n_yes: int
    n_no: int
    t: float               # Welch's t statistic
    strength: str          # "clear", "likely" or "unclear"


def compare(yes: Sequence[float], no: Sequence[float], label: str, min_n: int = 5) -> Effect | None:
    """Difference in means with Welch's t. With small personal samples this is a hint, not proof:
    'clear' needs |t| ≥ 2.5, 'likely' |t| ≥ 1.7; anything weaker is 'unclear'."""
    a, b = [v for v in yes if v is not None], [v for v in no if v is not None]
    if len(a) < min_n or len(b) < min_n:
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b)) or 1e-9
    t = (ma - mb) / se
    strength = "clear" if abs(t) >= 2.5 else "likely" if abs(t) >= 1.7 else "unclear"
    return Effect(label, round(ma - mb, 1), len(a), len(b), round(t, 2), strength)
