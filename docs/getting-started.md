# Getting started

From a fresh clone to your own dashboard in about 15 minutes, plus the import time for your
history (usually a few minutes).

## 1. Prerequisites

- macOS or Linux. The scheduled jobs (brief, coach, phone page) use macOS `launchd`. Everything
  else is cross-platform.
- [uv](https://docs.astral.sh/uv/getting-started/installation/): `brew install uv` or
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. It installs a suitable Python by itself.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) to drive the MCP server. `fitdash` itself
  doesn't need Claude, except for the AI coach notes.
- Data access through one of these routes (details in
  [mcp-server.md §1](mcp-server.md#1-which-route-can-you-use)):
  - **Google Health API**: an approved Google Cloud project, or the `ghealth` CLI already set up
    in `~/.config/ghealth/`.
  - **Legacy Fitbit Web API**: an existing developer app. Google is turning this API off on
    2026-10-30.
  - **Google Takeout**: always works. It's a one-off export, so there's no automatic sync.

## 2. Install

```sh
git clone https://github.com/pranshavpatel/fitbit-mcp.git
cd fitbit-mcp
uv sync                       # the server's virtual environment, from uv.lock
uv run pytest                 # optional: 76 offline tests
sh bin/install-fitdash.sh     # installs ~/.local/bin/fitdash (re-run if you move the folder)
```

If `~/.local/bin` isn't on your `PATH`, the installer tells you what to add to `~/.zshrc`.

## 3. Your data home

Everything personal lives outside the repo, in `~/.fitbit-mcp` (override with `FITBIT_MCP_HOME`):

```text
~/.fitbit-mcp/
  fitbit.sqlite3             the database the server imports into and fitdash reads
  config.json                server settings (copy config.example.json)
  credentials/               OAuth tokens (never shared, never committed)
  coaching/stats.json        fitdash settings: goals, split, habits, timezone, …
  coaching/profile.md        optional: your goals in your own words, read by the coach
  coaching/notes.json        coach notes
  lift_log.json, journal.json, training_log.json
  web/index.html             the phone page, if enabled
```

```sh
mkdir -p ~/.fitbit-mcp/coaching && chmod 700 ~/.fitbit-mcp
cp config.example.json ~/.fitbit-mcp/config.json
```

## 4. Connect and import

1. Run `claude` in the repo folder and approve the **fitbit-local** project MCP server. `/mcp` should
   list 13 tools.
2. Say **"Connect my Google Health account"** (or Fitbit, or "Import my Takeout from ~/Downloads/…").
   Credentials setup for each route is in [mcp-server.md §3](mcp-server.md#3-configure-a-provider-credentials-stay-outside-the-repo).
3. Say **"Import all my history"**. It runs as a background job, and you can ask "how's the import going?".
4. Later, "sync new data" brings it up to date. `fitdash` also syncs by itself when the data is over
   30 minutes old.

Google Cloud consent screens in *Testing* mode expire refresh tokens after 7 days. When syncing stops,
run `ghealth auth login` (CLI route) or say "connect Google with the browser" again.

## 5. Personalize fitdash

Create `~/.fitbit-mcp/coaching/stats.json`. Every key is optional:

```json
{
  "timezone": "Europe/London",
  "bedtime_goal": "23:30",
  "wake_anchor": "07:30",
  "sleep_need": "8:00",
  "steps_goal": 10000,
  "gym_goal": 4,
  "split": ["push (chest/tri)", "pull (back/bi)", "legs/abs", "arms"],
  "split_anchor": {"date": "2026-10-05", "day": "push (chest/tri)"},
  "impact_types": ["RUNNING", "TRAIL_RUN", "SOCCER", "TREADMILL_RUNNING"],
  "experiment": {"name": "Fixed wake time", "start": "2026-10-08", "nights": 14, "lights_out": "00:00"},
  "habits": [
    {"key": "alcohol", "label": "alcohol", "good": false},
    {"key": "mobility", "label": "mobility", "good": true},
    {"key": "creatine", "label": "creatine", "good": null}
  ],
  "brief_time": "08:00"
}
```

| Key | Meaning |
| --- | --- |
| `timezone` | IANA zone for every date and clock time. Default: `$FITDASH_TZ`, then this key, then your computer's zone. |
| `bedtime_goal`, `wake_anchor` | Drive tonight's "asleep by" time and the bed & wake chart. |
| `sleep_need` | Your base sleep need. Leave it out to learn it from your nights (see [scores.md](scores.md#sleep)). |
| `split`, `split_anchor` | Your lifting rotation. The anchor is one session you know, and later lifts follow the queue. |
| `impact_types` | Workout types counted for the running/jumping spike warning. |
| `experiment` | A sleep experiment tracked night by night. |
| `habits` | The journal's habits: `good` true = do, false = avoid, null = just track. |
| `brief_time` | When the morning notification runs (default: 30 min after `wake_anchor`). |

Also write a few lines about your goals, schedule and any injuries in `coaching/profile.md`. The
coach reads it.

## 6. Use it

```sh
fitdash                       # the whole dashboard
fitdash --section today       # just the summary
fitdash lift bench 3x8@60     # log a lift
fitdash journal               # answer today's habits
```

Next: [the fitdash reference](fitdash.md) · [coach, brief & phone page](automation.md) ·
[how the scores work](scores.md).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `fitdash` says "No data to show" | Connect and import first; check `~/.fitbit-mcp/fitbit.sqlite3` exists. |
| "sync skipped: … token expired" | `ghealth auth login`, or reconnect with the browser in Claude Code. |
| Times are off by hours | Set `"timezone"` in `stats.json` or `FITDASH_TZ`. |
| Recovery shows "—" | Needs overnight HRV and resting HR, which need the band worn overnight. |
| `Operation not permitted` over SSH (macOS) | The repo is in Desktop/Documents/Downloads. Give `/usr/libexec/sshd-keygen-wrapper` Full Disk Access, or clone somewhere else. |
| Coach notes are marked "auto" | The `claude` CLI wasn't found or failed. See [automation.md](automation.md#coach-notes). |

More server-side fixes: [mcp-server.md §10](mcp-server.md#10-troubleshooting).
