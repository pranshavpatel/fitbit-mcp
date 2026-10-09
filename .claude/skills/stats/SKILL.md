---
name: stats
description: Full terminal dashboard of the user's Fitbit / Google Health data (fitbit-local MCP), styled like WHOOP / Garmin. Shows Recovery %, Strain 0–21 and Sleep Performance rings, a sleep hypnogram, bed/wake timing, HRV and resting-HR trends, HR zones, steps and activity, workouts with per-session strain, acute:chronic load, impact spikes, body weight and lean-bulk pace, nutrition, the sleep experiment tracker, a lift log (sets per muscle vs 10–20/week, per-lift 1RM progress), journal & habit tracking, "what drives your recovery" correlations and a scheduled morning notification. Use it whenever the user asks for their stats, dashboard, fitdash, "show me everything", recovery or strain scores, a weekly or monthly review, or a chart of any Fitbit metric in the terminal, logging sets ("I did bench 3x8 at 60"), training volume, habits or a journal entry ("had a beer", "knee hurts"), what affects their recovery, or a morning brief, even if they don't say "stats". For a short plan for the day with coaching, use daily-health-brief instead.
---

# stats: the fitdash dashboard

The script draws everything. Your job is to sync, run it, show its output exactly as printed,
and add at most three lines of coaching.

With no flags it shows everything, in a fixed order:
1. A **Today** box: the rings, one verdict, six key numbers with 14-day trends, "Needs
   attention" (only ! and ✕ items), and tonight's / this week's plan.
2. One box per area: Sleep, Sleep experiment, Recovery & heart, Muscle Freshness, Training plan,
   Workouts, Strain & activity, This week vs last, Body & nutrition. In the two-column layout the
   "This week vs last" box goes to whichever column is shorter, so neither leaves a big gap.
3. A quiet Data footer.

Each box follows the same hierarchy: a headline (status icon + the number that matters), then
one or two charts, then dim secondary details. The user found an all-panels grid with equal
weight everywhere too cluttered, and didn't want to remember flags, so run it with no flags
unless they ask for one area or a week/month review.

## 1. Sync (when the fitbit-local tools are available)

1. `mcp__fitbit-local__sync_data` with `provider: "google"`, `overlap_days: 2`.
2. Poll `mcp__fitbit-local__job_status` until it finishes. A sync usually takes under 30 seconds.
3. If it **fails**, render anyway. The header shows the last sync time and flags data older than
   6 h as stale. In one line, say what failed. A 7-day token expiry is fixed with
   `! ghealth auth login`. Never say data is current when it isn't.

When the user runs `fitdash` themselves in a terminal, it refreshes on its own: if the last sync
is over 30 minutes old (`FITDASH_SYNC_MAX_AGE` minutes), it runs `scripts/sync_now.py` in the
project environment, which uses the same code path as the `sync_data` tool. `--sync` forces a sync
and `--no-sync` skips it. When you run the script from Bash, stdout isn't a terminal, so it never
syncs on its own; keep syncing through the MCP tools as above.

## 2. Render

```bash
uv run --quiet --script <skill>/scripts/dashboard.py --no-color --width 80 [flags]
```

`<skill>` is this skill's directory. Choose the flags from the request:

| Request | Flags |
|---|---|
| "my stats", "show me everything", dashboard | none |
| only one area ("just my sleep"), or a deep dive into one ("analyze my sleep") — a single section shows an extended view: 8 weeks and extra boxes (deep.py) | `--section today\|sleep\|recovery\|freshness\|strain\|workouts\|training\|week\|body\|logs` |
| muscle recovery, "which muscles are fresh" | `--section freshness` (add `--sort freshness` for least-recovered first) |
| "I did pull yesterday" | run `<script> tag yesterday pull`, then render |
| "I did bench 3x8 at 60 and rows 4x10 at 50" | run `<script> lift bench 3x8@60 row 4x10@50` (`yesterday` / a date first for past days; `25lb` for pounds; no `@` for bodyweight), then `--section lifting` |
| sets per muscle, "am I doing enough volume", lift progress | `--section lifting` |
| "what affects my recovery", "why is my recovery low" | `--section insights` |
| "I had two beers", "stretched tonight", "knee hurts", a journal note | run `<script> journal +alcohol` / `+stretch` / `+knee-pain` / `note "…"` (`yesterday` or a date first for another day; before noon the default is yesterday), then `--section journal`. `<script> journal habits` lists the keys |
| "fitdash on my phone", phone app | the phone app runs via `<script> web --install` (status: `web --status`; foreground: `<script> app`); the user opens it over Tailscale (`tailscale serve --bg 8787`). It shows everything and logs habits and lifts. Their data may be public if they ask, but never expose the app with `tailscale funnel` without authentication: it has write endpoints |
| "my priorities today are …", "I finished X", "how did my priorities go" | `<script> priorities set "…" "…" "…"` / `priorities done 1` (`some`, `missed`) / `priorities reflect "…"`, then `--section today`. Only record what the user said. Never mark a priority done on their behalf without being told |
| "coach me", "write my coach note", a note for the Today box | `<script> coach run --kind morning\|activity\|evening --force`, or write it yourself and save it with `<script> coach set <kind> "text"` |
| morning notification / "brief me every morning" | `<script> brief --install [HH:MM]` (status: `brief --status`, remove: `brief --uninstall`) |
| this week / month, trends, "how did I do" | `--period week` or `--period month` |
| a past day | `--date YYYY-MM-DD` |
| longer trend charts | `--days 42` (range 14–90) |

- **Width.** Your reply is indented inside the terminal, so leave a margin. Use `--width 80`
  unless the user has told you their terminal width; in that case use their width minus 4.
  From 140 columns the boxes sit in two columns: Sleep, Sleep experiment, Recovery & heart and
  Body & nutrition on the left, and Muscle freshness, Training plan, Workouts and Strain & activity
  on the right (muscle building is the user's main goal, so it leads that column).
  The Today box spans both and puts its key numbers beside Needs attention and Plan. Below 140
  it's one column, at most 100 wide.
- **Color.** Bash output reaches you with ANSI codes as visible junk, and your reply can't show
  color. Always pass `--no-color` here. Every color cue also has a glyph or shape (● ◐ ○, ✓ ! ✕,
  █ ▓ ▒ ░, ▲), so nothing is lost.
- **Showing it.** Paste the output **verbatim** in a ```` ```text ```` fence. Never retype,
  realign, trim or summarize it, and never add your own text inside a panel: every column is
  computed. Your coaching goes below the fence.
- **Muscle Freshness** (`--section freshness`, optionally `--sort freshness`) is a card grid
  with half-block pixel icons and truecolor dots, designed to be seen in color. After pasting the
  plain version, remind the user that `! fitdash --section freshness` shows it in full color.
- **Full color.** The first time in a conversation, tell the user they can run `fitdash` (same
  flags) in their own terminal for color, sized to the window.
- **Raw numbers.** If you need values for the coaching that the picture doesn't make clear, run
  again with `--json`.

## 3. Coach in three lines at most

Base it on the verdict and the ✓ / ! / ✕ lines, plus `~/.fitbit-mcp/coaching/profile.md` if it
exists: goals, the split, bedtime goal and knee notes. Each line should be one concrete action
for today or tonight, with no filler. If an impact spike is flagged, mention the knee before
suggesting any running or jumping. This isn't medical advice: for something persistent or
worrying, suggest a clinician.

## What the numbers are

All formulas live in `scripts/scores.py`, and the footer shows the version. These are estimates
on WHOOP-style scales calibrated on the user's own history, **not WHOOP's proprietary algorithm**.

- **Recovery 0–100 %.** Weighted z-scores against the prior 28 days: overnight HRV 45 %, resting
  HR inverted 30 %, sleep score 25 %. Penalties apply when respiratory rate or skin
  temperature sits more than 1.5 SD off baseline. The total goes through a logistic curve, so
  every input at baseline gives about 56 %. Bands: green ≥ 67, yellow 34–66, red ≤ 33. Without
  overnight HRV or resting HR the score is "—", never a guess.
- **Strain 0–21.** Banister TRIMP on heart-rate reserve. It uses all-day HR when that covers
  18 h, otherwise Active Zone Minutes. Zone intensities are calibrated from the user's own Fitbit
  zone limits, and HRmax is the 99.5th percentile of recorded HR. The scale is logarithmic:
  21 · ln(1 + T/25) / ln(21). Target ranges: green 14–18, yellow 10–14, red 4–10.
- **Sleep score 0–100** (shown on the Sleep ring, the Today row and the Sleep box). Modeled on
  WHOOP's 2025 Sleep Performance, which has four components; WHOOP doesn't publish weights, so
  these are ours: hours vs need 50 % (asleep ÷ need, capped at 100), efficiency 20 % (asleep ÷ in
  bed), consistency 15 % (bed/wake regularity over the last 4 nights) and sleep stress 15 %
  (100 − % of the night's 5-minute HRV readings below the user's own 20th percentile from the
  previous 28 nights). Missing parts are re-weighted. Bands: ≥ 90 optimal, 70–89 sufficient,
  < 70 poor. Need = personal baseline + strain adjustment + ½ of sleep debt (max 1 h) − nap
  credit. Baseline = 75th percentile of the last 28 nights, clamped to 7–9 h, leaving out rebound
  nights (any night after one under 6 h), or `"sleep_need": "8:00"` in stats.json when set. Sleep
  debt is a running 7-night balance: short nights add, long nights pay back, older debt fades 15 %
  a night. Recovery uses this score as its sleep input.
- **Load ratio.** Mean daily TRIMP over 7 days ÷ over 28 days. 0.8–1.3 is the sweet spot;
  above 1.5 is a warning.
- **Impact spike.** A week of running + soccer more than 1.3× the previous 4-week average
  (and ≥ 60 min).
- **Tonight's bedtime.** Sleep need going into tonight (baseline + today's strain + ½ of the
  sleep-debt balance including last night) counted back from the wake anchor. The experiment's lights-out time is shown as the latest limit.
- **Wear coverage.** A day with under 10 h of activity-level plus sleep minutes counts as not
  worn: its strain is a gap (·), never a rest day, so it can't drag down the load ratio.
- **Muscle freshness.** A fatigue model in `scripts/scores.py`. Each session adds fatigue to the
  muscles it worked: dose = 70 · √(minutes/60) · √(strain/8), capped at 100. Primary muscles take
  the full dose and secondary muscles half (push → chest; shoulders and triceps secondary; pull →
  lats; biceps secondary, rear delts ¼). Runs, soccer and plyometrics load quads, hamstrings,
  glutes and calves at 0.6–0.8. Fatigue halves every 48 h for big muscles (chest, lats, quads,
  hamstrings, glutes) and 36 h for small ones, ×1.25 faster after a green Recovery and ×0.75
  slower after a red one. Freshness = 100 − remaining fatigue. Sessions older than 7 days don't
  count. With no strength session in 30 days the panel says "no strength sessions logged"
  instead of showing percentages.
- **What a lift trained.** Fitbit doesn't record it. `~/.fitbit-mcp/training_log.json` holds
  what the user tagged (`fitdash tag <date> <split day>`, e.g. `fitdash tag yesterday pull`).
  Untagged lifts after `split_anchor` follow the split rotation and are shown as "assumed". When
  the user tells you what they trained, run the tag command rather than editing files by hand.
- **Training zones.** 5 zones by % of HRmax (50/60/70/80/90 %). Each workout's badge is the zone
  with the most time, from HR samples when they cover the session, else from average HR. For
  lifting, HR zones understate effort, so the badge is dimmed.
- **Lift log.** `fitdash lift bench 3x8@60 row 4x10@50` writes `~/.fitbit-mcp/lift_log.json`
  (`fitdash lift` shows today, `fitdash lift undo` removes the last entry). Each exercise has
  primary muscles (1 set per hard set) and secondary ones (½), e.g. bench → chest; triceps and
  shoulders ½. The Lifting box compares hard sets per muscle over the last 7 days with the
  10–20/week hypertrophy range and tracks each lift's best estimated 1RM per session (Epley,
  w·(1+reps/30)) over 8 weeks; ★ PR is a new best. On days with logged sets, Muscle Freshness uses
  the real sets (dose = 70·√(sets/8) per muscle, capped at 100) instead of the split guess, and the
  Fitbit lift that day isn't counted twice. When the user tells you what they lifted with sets and
  weights, log it with the `lift` command rather than editing the file.
- **What drives your recovery.** For each habit (asleep by 1:00, 7 h+ asleep, asleep within 45 min
  of the usual time, strain at the user's 75th percentile (≥ 12) the day before, a workout past 20:00,
  lifting, 10k+ steps) it compares average next-morning Recovery with vs without, using Welch's t:
  clear |t| ≥ 2.5, likely ≥ 1.7, else unclear. It needs 21+ mornings and 5+ on each side. Always
  call it correlation in the user's own data, not proof: the habits travel together, and Recovery
  includes the sleep score, so the sleep rows partly measure themselves.
- **Journal & habits.** `fitdash journal +stretch -alcohol note "…"` writes
  `~/.fitbit-mcp/journal.json`; `+key` = did it, `-key` = didn't, keys match by prefix; alone in a
  terminal it asks each habit. Defaults: alcohol, caffeine after 2pm, ate within 2 h of bed, screens
  in bed (avoid); stretching, protein goal (do); creatine (track); knee pain (avoid). The user can
  change them under `"habits"` in stats.json. An entry is about the day it happened and is compared
  with the next morning's Recovery, both in the Journal box ("next AM") and as extra rows in What
  drives your recovery, once there are 5+ days each way. Log only what the user actually told you;
  never infer a habit (e.g. don't mark alcohol "no" because they didn't mention it).
- **Morning brief.** `fitdash brief` syncs, then shows one macOS notification (and prints the
  same): Recovery, sleep score and hours, the strain target, the next split day and its freshness,
  the knee note when there's an impact spike, tonight's asleep-by time and muscles under 10 sets.
  `--install` adds a launchd agent (`~/Library/LaunchAgents/com.fitdash.brief.plist`), by default
  30 min after `wake_anchor` (override with `brief_time` in stats.json); output goes to
  `~/.fitbit-mcp/brief.log`. Only install or uninstall it when the user asks.
- **Phone app.** `appserver.py` (stdlib HTTP on 127.0.0.1:8787) serves `app/` (plain HTML/CSS/JS,
  five tabs: Today, Sleep, Training, Log, Trends), `/api/model` (the same model as `--json`) and
  writes to the journal and lift log (JSON + `X-Fitdash: 1` header, no CORS). The launchd agent
  from `web --install` runs it; the terminal-style page stays at `/terminal`.
- **Priorities.** Up to 3 a day in `~/.fitbit-mcp/priorities.json` (until 04:00 it's still the day
  before). `fitdash` asks for them in a terminal in the morning (04:00–14:00) and for a review in
  the evening (from 19:00); skipping once silences it for the day. The Today box and the app
  show them. The coach's morning, midday (13:00–16:00, if any are open) and evening notes refer to
  them by name.
- **Stress 0–3.** Needs all-day heart rate. With workout-window HR only, it shows "—" and says
  why.

## Settings

`~/.fitbit-mcp/coaching/stats.json` holds the bedtime goal and step target, wake anchor, step
and gym goals, the split order, `split_anchor`, impact workout types, the sleep experiment and optional `brief_time`. Lift tags live in
`~/.fitbit-mcp/training_log.json` (written by `fitdash tag`) and logged sets in
`~/.fitbit-mcp/lift_log.json` (written by `fitdash lift`), the journal in `~/.fitbit-mcp/journal.json`. The data can't tell which muscle group a strength session trained. `split_anchor`
(`{"date": "YYYY-MM-DD", "day": "<split day>"}`) records one known session, and the queue
advances by one for every later strength session. If the user tells you what they lifted and
it doesn't match the queue, tag it with `fitdash tag`.
Keep settings and logs in `~/.fitbit-mcp` (not the repo) so the code stays separate from personal files.

## Development

Tests: `uv run --no-project --with rich --with pytest pytest -q .claude/skills/stats/tests`.
They use a synthetic fixture DB built on the real schema and render every section at 80, 120
and 160 columns with `STATS_STRICT=1`, so any line wider than its panel fails.
