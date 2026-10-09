# How the scores work

Every formula is plain Python in [`scores.py`](../.claude/skills/stats/scripts/scores.py). The
dashboard footer shows the formula version. The scales imitate WHOOP's so they feel familiar, but
they are **estimates calibrated on your own history, not WHOOP's proprietary algorithm**, and they
are not medical measurements.

Baselines are always your previous 28 days. A value with too little data shows "—" and is never
filled in with a guess.

## Recovery 0–100 %

| Input | Weight | Direction |
| --- | --- | --- |
| Overnight HRV (RMSSD) | 45 % | higher is better |
| Resting heart rate | 30 % | lower is better |
| Sleep score | 25 % | higher is better |

Each input becomes a z-score against your 28-day baseline (with small floors on the standard
deviation so a very steady week doesn't blow up the scale). The weighted sum goes through a
logistic curve, so every input exactly at baseline gives about 56 %. Respiratory rate or skin
temperature more than 1.5 SD off baseline subtracts a penalty, which often shows up before
illness.

Bands: **green ≥ 67**, **yellow 34–66**, **red ≤ 33**. Without overnight HRV or resting HR the score
is "—".

## Strain 0–21

1. **Load (TRIMP).** Banister's training impulse on heart-rate reserve: minutes × intensity ×
   e^(1.92 × intensity). It uses minute-by-minute heart rate when that covers 18 h of the day, and
   otherwise Fitbit's Active Zone Minutes. Zone intensities come from **your** Fitbit zone limits,
   and HRmax is the 99.5th percentile of all your recorded heart rate.
2. **Scale.** `21 · ln(1 + TRIMP/25) / ln(21)`, logarithmic like WHOOP's, so going from 18 to 20
   takes far more work than going from 8 to 10.
3. **Target for today** depends on Recovery: green 14–18, yellow 10–14, red 4–10.

## Sleep

**Sleep need** = baseline + a strain adjustment for yesterday + ½ of your sleep debt (at most
1 h) − credit for naps.

- **Baseline**: the 75th percentile of the last 28 nights, which is roughly what you sleep when
  nothing cuts the night short, clamped to the 7–9 h adult range. Nights right after one under 6 h
  are left out: they're *rebound* sleep, your body repaying debt, and with an irregular schedule
  they'd make your normal need look much higher than it is. You can set it yourself with
  `"sleep_need": "8:00"` in `stats.json`, which is worth doing until you have a few regular weeks.
- **Sleep debt**: a running balance over the last 7 nights. A night under your baseline adds the
  shortfall, a night over it pays debt back, the balance never goes below zero, and older debt fades
  15 % per night. Only half is added to tonight's need, because one night can't repay it all.

**Sleep score 0–100** has the four components WHOOP describes for its 2025 Sleep Performance. WHOOP
doesn't publish the weights, so these are ours:

| Component | Weight | Measures |
| --- | --- | --- |
| Hours vs need | 50 % | asleep ÷ need, capped at 100 |
| Efficiency | 20 % | asleep ÷ time in bed |
| Consistency | 15 % | how regular bed and wake times were over the last 4 nights |
| Sleep stress | 15 % | 100 − % of the night's 5-minute HRV readings below your own 20th percentile |

Missing parts are re-weighted. Bands: ≥ 90 optimal, 70–89 sufficient, < 70 poor.

**Tonight's "asleep by"** = tonight's need counted back from your `wake_anchor`. Tonight's need
includes today's strain and half of the debt balance, including last night.

## Training load

- **Load ratio (ACWR).** Mean daily TRIMP over 7 days ÷ over 28 days. 0.8–1.3 is the sweet spot;
  above 1.5 is a warning.
- **Impact spike.** A week of running, soccer and similar (`impact_types`) more than 1.3× the
  previous 4-week average and at least 60 min. The dashboard then shows a weekly minute limit.
- **Wear coverage.** A day with under 10 h of activity-level plus sleep minutes counts as *not
  worn*. Its strain is a gap, never a rest day, so it can't drag the load ratio down.
- **HR zones.** Five zones at 50/60/70/80/90 % of HRmax. Each workout's badge is its zone with
  the most minutes.

## Muscle freshness

A fatigue model per muscle (chest, shoulders, lats, biceps, triceps, abs, quads, hamstrings,
glutes, calves):

- **From logged sets** (`fitdash lift`): dose per muscle = 70 · √(sets / 8), capped at 100. A hard
  set counts 1 for the exercise's primary muscles and ½ for secondary ones.
- **From a Fitbit session without a log**: dose = 70 · √(minutes/60) · √(strain/8). The split day
  decides the muscles (primary full, secondary ½, minor ¼). Runs and soccer load the legs at 0.6–0.8.
- **Decay**: fatigue halves every 48 h for big muscles and every 36 h for small ones. It decays
  ×1.25 faster after a green Recovery and ×0.75 slower after a red one. Sessions older than 7 days
  don't count.
- **Freshness** = 100 − remaining fatigue.

## Lifting

- **Weekly volume**: hard sets per muscle over the last 7 days, compared with the 10–20 sets/week
  range commonly cited for hypertrophy.
- **Progress**: each session's best estimated 1RM per lift (Epley: weight × (1 + reps/30)) over
  8 weeks. ★ PR marks a new best. Bodyweight sets count toward volume but have no 1RM.

## Insights: what drives your recovery

For each habit (asleep by 1:00, 7 h+ asleep, regular bedtime, a hard day before, a workout ending
after 20:00, lifting, 10k+ steps, and every journal habit), mornings are split into "did it the
day/night before" and "didn't". It then compares average Recovery with Welch's t-test:

- **clear** |t| ≥ 2.5, **likely** |t| ≥ 1.7, otherwise **unclear**;
- at least 5 mornings on each side and 21 mornings overall.

This is correlation in a small personal sample, not proof. Habits travel together (late nights,
hard days and short sleep), and Recovery includes the sleep score, so the sleep rows partly measure
themselves.
