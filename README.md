# fitbit-mcp + fitdash

**Your own Fitbit / Google Health data, on your own machine: a read-only MCP server for Claude, and
`fitdash`, a WHOOP-style terminal dashboard with recovery, strain, sleep, muscle freshness, a lift
log, habit tracking and an AI coach.**

![The full fitdash dashboard](docs/demo/dashboard.svg)

<sub>All screenshots are generated from synthetic data by [`docs/demo/make_demos.py`](docs/demo/make_demos.py).</sub>

## What's in here

| Part | What it does |
| --- | --- |
| **[fitbit-local MCP server](docs/mcp-server.md)** | Connects your Google Health (or legacy Fitbit) account read-only, imports your full history into a local SQLite database, keeps it synced, and gives Claude 13 tools to query, summarize and export it. Also imports Google Takeout. |
| **[`fitdash`](docs/fitdash.md)** | A terminal dashboard on top of that database: Recovery %, Strain 0–21 and Sleep score rings, sleep stages and timing, HRV / resting HR trends, workouts and HR zones, training load, muscle freshness, lifting volume and progress, habits, body weight and more. |
| **[Phone app, coach & brief](docs/automation.md)** | A WHOOP-style phone app (view everything, log habits and lifts) served from your computer over Tailscale; a Claude-written coach note in the morning, after workouts and in the evening; and a 9 a.m. notification. |
| **[Claude Code skills](.claude/skills)** | `/stats` (sync, show the dashboard, coach) and `daily-health-brief`, so you can just ask Claude "show me my stats" or "I did bench 3x8 at 60". |

## A quick look

| Today: rings, verdict, coach note, key numbers | Muscle freshness from your actual sets |
| --- | --- |
| ![Today box](docs/demo/today.svg) | ![Muscle freshness](docs/demo/freshness.svg) |
| **Lifting: sets per muscle vs 10–20 / week, 1RM progress** | **What drives *your* recovery** |
| ![Lifting](docs/demo/lifting.svg) | ![Insights](docs/demo/insights.svg) |
| **Journal & habits, linked to next-morning recovery** | **Sleep score, stages and 14 nights of timing** |
| ![Journal](docs/demo/journal.svg) | ![Sleep](docs/demo/sleep.svg) |

### Deep dives

Ask for one section and you get an extended view: 8 weeks of history and extra charts, such as
stages night by night, bed & wake statistics, a recovery calendar, recovery vs yesterday's strain,
an hour-by-day activity heatmap, 6 weeks of lifting volume and when each muscle will be ready.

| `fitdash --section sleep` | `fitdash --section lifting` |
| --- | --- |
| ![Sleep, extended](docs/demo/sleep_extended.svg) | ![Lifting, extended](docs/demo/lifting_extended.svg) |

### The phone app

A WHOOP-style web app served by your own computer and opened over Tailscale. It shows everything
the dashboard does, and you can tap to log habits and lifts.

| Today | Sleep | Training | Log | Trends |
| --- | --- | --- | --- | --- |
| <img src="docs/demo/app_today.png" width="150"> | <img src="docs/demo/app_sleep.png" width="150"> | <img src="docs/demo/app_train.png" width="150"> | <img src="docs/demo/app_log.png" width="150"> | <img src="docs/demo/app_trends.png" width="150"> |

## Quick start

You need macOS or Linux, [uv](https://docs.astral.sh/uv/) and data access through **one** of: an
approved Google Health API project (or the `ghealth` CLI), a working legacy Fitbit app, or a
Google Takeout export. [Which one can I use?](docs/mcp-server.md#1-which-route-can-you-use)

```sh
git clone https://github.com/pranshavpatel/fitbit-mcp.git && cd fitbit-mcp
uv sync                              # the MCP server's environment
sh bin/install-fitdash.sh            # puts `fitdash` in ~/.local/bin
claude                               # approve the "fitbit-local" server when asked
```

Then, in Claude Code:

1. "Connect my Google Health account", then "Import all my history". Claude reports success only
   once the import job has actually finished.
2. "Show me my stats", or run `fitdash` in any terminal. It syncs first if the data is over 30 min old.

Want to see it without an account? `uv run --script docs/demo/make_demos.py` builds a synthetic
data home and renders every view.

The step-by-step version, with settings and troubleshooting, is in
**[docs/getting-started.md](docs/getting-started.md)**.

## Everyday use

```sh
fitdash                                    # everything: Today box, then every area in its own box
fitdash --section sleep                    # one area, extended: 8 weeks + extra charts (recovery, lifting, …)
fitdash --period week                      # this week vs last
fitdash lift bench 3x8@60 row 4x10@50      # log sets (kg; 25lb for pounds; no @ = bodyweight)
fitdash journal +mobility -junk-food note "slept well"
fitdash priorities set "ship the report" "pull day" "call mom"   # today's top 3; the coach checks in
fitdash coach run                          # a Claude-written note if one is due
fitdash brief --install                    # a notification every morning
fitdash web --install                      # the phone app, running in the background
```

Full reference: [docs/fitdash.md](docs/fitdash.md).

## How the numbers work

Recovery, Strain and Sleep score use WHOOP-like scales, calibrated on **your** history. They are
not WHOOP's proprietary algorithm. Every formula is plain Python in
[`scores.py`](.claude/skills/stats/scripts/scores.py) and documented in
[docs/scores.md](docs/scores.md). In short:

- **Recovery 0–100 %**: overnight HRV (45 %), resting HR (30 %) and sleep score (25 %) as z-scores
  against your previous 28 days, through a logistic curve.
- **Strain 0–21**: Banister TRIMP from heart-rate zones calibrated to your own Fitbit zones, on a
  log scale.
- **Sleep score**: hours vs need (50 %), efficiency (20 %), consistency (15 %), sleep stress (15 %).
- **Muscle freshness**: a fatigue model with per-muscle half-lives, fed by your logged sets.
- **Insights**: next-morning recovery with vs without each habit (Welch's t). Correlation, not proof.

## Design principles

- **Local first.** Data lives in `~/.fitbit-mcp`. The server only ever makes read-only (GET and
  read-only query) calls to Google/Fitbit, with `.readonly` scopes.
- **Honest numbers.** Missing days are gaps, not zeros. A score with no inputs shows "—", never a
  guess. Low-wear days don't count as rest days.
- **No free-form SQL.** Every query is parameterized, and the tools expose fixed operations only.
- **Not medical advice.** These are estimates for training and habits. Talk to a clinician about
  anything that worries you.

## Project layout

```text
src/fitbit_mcp/          the MCP server (OAuth, importer, jobs, store, queries, summaries, Takeout)
tests/                   server tests (synthetic fakes of the Google/Fitbit APIs)
.claude/skills/stats/    fitdash: scripts/ (dashboard, scores, data, charts, coach, …) and tests/
.claude/skills/daily-health-brief/   a lighter one-screen brief
docs/                    guides, plus demo/ (screenshots and the script that makes them)
bin/install-fitdash.sh   installs the `fitdash` command
.claude/skills/stats/app/  the phone app (HTML/CSS/JS, no build step)
```

More in [docs/architecture.md](docs/architecture.md), including how to add a metric or a section.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Both test suites
run offline on synthetic data:

```sh
uv run pytest                                                                         # server: 76 tests
uv run --no-project --with rich --with pytest pytest .claude/skills/stats/tests       # fitdash: 312 tests
```

## License

[MIT](LICENSE). Fitbit and Google Health are trademarks of Google. WHOOP is a trademark of WHOOP,
Inc. This project isn't affiliated with any of them.
