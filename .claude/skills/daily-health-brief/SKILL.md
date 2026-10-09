---
name: daily-health-brief
description: Daily health summary and coaching from the user's own Fitbit / Google Health data (fitbit-local MCP), with terminal charts for sleep stages, sleep history, resting HR, HRV, steps and training load, followed by a concrete plan for the day. Use this whenever the user asks how they're doing today, for their daily summary, daily brief, health check-in, readiness, "should I train today", "how did I sleep", "what should I do today" about workouts or recovery, or wants their Fitbit stats shown as charts in the CLI, even if they don't say "brief". Not for calendar/schedule morning briefs.
---

# Daily health brief

You're producing two things: a **dashboard** the user can glance at (charts rendered by a script),
and **coaching** for the rest of their day that's grounded in those numbers and their stated goals.
The script does all the arithmetic, so the coaching can focus on judgment.

## 1. Get fresh data

The dashboard reads only the local database, so sync first. This only reads from Google.

1. `mcp__fitbit-local__sync_data` with `provider: "google"`, `overlap_days: 2`. The overlap
   picks up last night's sleep and late-arriving daily values.
2. Poll `mcp__fitbit-local__job_status` with the returned `job_id` until it finishes. A sync
   usually finishes in under 30 seconds. While you wait, give the user a one-line update.
3. If the job **fails**, don't present the brief as current. Check `connection_status`. The usual
   cause is the Google refresh token expiring after 7 days while the consent screen is in Testing
   mode. The fix is `! ghealth auth login`. Then either stop and ask, or continue with local data
   and label it "as of <last sync time>". If it finishes **partial**, name the categories that
   are missing.

## 2. Render the dashboard and the numbers

Run the script in one Bash call. `<skill>` is this skill's directory:

```bash
python3 <skill>/scripts/dashboard.py --plain; echo '=====DIGEST====='; python3 <skill>/scripts/dashboard.py --json
```

- `--plain` gives the charts without ANSI color, so they display correctly inside a code block.
- `--json` gives the same numbers as structured data: readiness signals and their levels, last
  night's sleep and stages, vitals with 28-day baselines, today's activity, today's workouts and
  the 7-day training totals. Use it for the coaching rather than re-reading values off the charts.
- Other options: `--date YYYY-MM-DD` for a past day and `--days 7..21` for the history width.
  Use them if the user asks about another day or a longer view.

Bash output doesn't reliably reach the user, so the charts only appear if you include them in
your reply. Copy the `--plain` output **verbatim** into a ```` ```text ```` fence. Don't realign,
trim or "fix" it, because every column is computed.

The first time in a conversation, mention once that they can run the color version themselves:
`! python3 .claude/skills/daily-health-brief/scripts/dashboard.py`. Add `--theme light` on a
light terminal.

## 3. Coach

Read `~/.fitbit-mcp/coaching/profile.md` if it exists. It holds the user's goals, preferences and
constraints, such as training split, bedtime goal and injury notes. The user's own words in this
conversation override it. The profile records **intentions**, not completed workouts, so never
treat a planned split day as done.

### What the readiness signals mean

The script grades each signal against the user's own recent baseline. These are heuristics for
planning training, not clinical thresholds:

| Signal | Good | Watch | Flag |
|---|---|---|---|
| Sleep last night | ≥ 7 h | 6–7 h | < 6 h |
| Resting HR vs 28-day avg | ≤ +2 bpm | +2 to +5 | ≥ +5 |
| HRV vs 28-day avg | ≥ −10 % | −10 to −20 % | < −20 % |
| Skin temp vs baseline | within 2 SD | beyond 2 SD | — |
| Training, last 3 days | ≤ 1.5× usual | > 1.5× usual | — |
| 3-night sleep average | — | < 6.5 h | — |

The overall verdict is the worst of sleep, resting HR and HRV. Skin temperature and load are
context only. One amber signal next to two green ones means adjust the plan, not cancel it.

### Shape of the coaching

Write it after the dashboard, under a short heading. Be direct and specific, with no
motivational filler. 120–250 words is usually enough.

1. **Verdict** in one sentence, naming the one or two numbers that drive it.
2. **Plan for the rest of today.** Give 3–5 concrete actions that fit the current time of day:
   a morning brief plans the whole day, an evening brief covers only tonight. Cover:
   - **Training.** What to do and how hard, based on readiness and the last 7 days. Use the
     user's split from the profile, but you can't tell from the data which muscle group they
     trained. Session types only say "Strength", so ask or offer options rather than guessing.
     If you'd suggest running or jumping and the profile mentions joint pain, check how it feels
     first, and never prescribe training through pain.
   - **Sleep tonight.** Work back from their bedtime goal. Mention the wind-down and caffeine
     cutoff only if last night or the 3-night average was short, or bedtime has been drifting late.
   - **Fueling.** One line tied to their goal, such as protein and carbs around training for a
     lean bulk. Keep it general and leave out calorie prescriptions.
3. **Tomorrow's check.** Name the specific number(s) that would change the plan, e.g. "if resting
   HR is back above ~60 or you sleep under 6 h, go easy."

### Honesty rules

- If `activity.partial_day` is true, today's steps and workouts are **so far**. Don't call a
  morning "low activity".
- Missing days are gaps, not zeros. If a baseline is `null` (fewer than 7 readings), say there
  isn't enough history to compare yet.
- Describe what changed together. Don't claim one thing caused another from a few days of data.
- This isn't medical advice. If something looks unusual and persistent, such as a sustained rise
  in resting HR, a skin temperature shift for several nights, SpO₂ that keeps dropping, or a
  symptom the user mentions, suggest talking to a clinician instead of interpreting it.

### Keeping the profile current

If the user states a new goal, schedule, bedtime or injury update, offer to update
`~/.fitbit-mcp/coaching/profile.md`. Edit only after they agree, and keep it to their own words
and the date. That file is local; never copy it or any health data into the repo.
