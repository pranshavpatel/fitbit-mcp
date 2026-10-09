# The fitbit-local MCP server

A local, read-only [MCP](https://modelcontextprotocol.io) server (stdio, official Python SDK v2) that
connects your own Fitbit account, imports your history into a local SQLite database, and lets you
explore it conversationally in Claude Code. Health data and credentials never leave your computer
except for the read-only requests to Fitbit/Google that you start.

> This guide covers the MCP server on its own. For the whole project (the `fitdash` dashboard,
> coach notes, phone page) start at the [README](../README.md).

## 1. Which route can you use?

Checked against official pages on 2026-10-07:

* **Legacy Fitbit Web API** — support ended 2026-09-30 and Google says the API **is turned off on
  2026-10-30**. Use it only with an *existing, working* Fitbit developer app, and start the import
  well before the shutdown. The server refuses Fitbit API calls on/after that date.
* **Google Health API** — the successor. All scopes are *restricted* and Google states it is **not
  onboarding new projects**. A new Google Cloud project will most likely not get access; use this
  route only if your project is already approved/eligible.
* **Takeout** — always available. Request a Google Takeout (select *Google Health* / *Fitbit*), download
  every part, then use `import_takeout`. This is the honest fallback when no API access exists.

| Your situation | Use |
| --- | --- |
| Approved Google Health API project | Google (default when configured) |
| Working Fitbit developer app, before Oct 30 | Fitbit legacy |
| Neither | Takeout |

Sources: <https://developers.google.com/health/about>, <https://dev.fitbit.com/build/reference/web-api/>,
<https://support.google.com/googlehealth/answer/14236615>.

## 2. Install

Requires [uv](https://docs.astral.sh/uv/) (it fetches a suitable Python ≥3.10 automatically; macOS's
system Python 3.9 is too old). From this folder:

```sh
brew install uv            # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                    # creates .venv from uv.lock (do this once before enabling the server)
uv run pytest              # offline test suite, synthetic data only
```

## 3. Configure a provider (credentials stay outside the repo)

All private files live in `~/.fitbit-mcp/` (override with `FITBIT_MCP_HOME`): `config.json`,
`client_secret.json`, `credentials/`, the database, raw responses, Takeout extracts, exports, logs.

```sh
mkdir -p ~/.fitbit-mcp && chmod 700 ~/.fitbit-mcp
cp config.example.json ~/.fitbit-mcp/config.json
```

### Google Health API — reuse a `ghealth` CLI setup (recommended)

If you already use the `ghealth` CLI (as health-coach-app does), there's nothing to configure:
fitbit-mcp reads the same setup in `~/.config/ghealth/`.

* **OAuth client:** `~/.config/ghealth/client_secret.json` (used when `~/.fitbit-mcp/client_secret.json`
  does not exist; override with `google_client_secrets` in config.json or `GOOGLE_CLIENT_SECRETS`).
* **Credentials:** `connect_account` (method `auto` / `ghealth_cli`) copies `~/.config/ghealth/credentials.json`
  into fitbit-mcp's own token file `~/.fitbit-mcp/credentials/google.json`, then verifies with a read-only
  `users/me/identity` call. The CLI's file is only read, never written, so the CLI, health-coach-app and
  fitbit-mcp can't corrupt each other's state. Google doesn't rotate refresh tokens, so sharing is safe.
* **Self-healing re-auth:** while the consent screen is in *Testing* mode Google expires refresh tokens
  after 7 days. When a refresh fails, the server retries once with the CLI's current credentials, so after
  `ghealth auth login` (the same weekly chore health-coach-app documents) it recovers by itself.
* **Day boundaries:** physical-time filters use local midnight converted to UTC, in the zone from
  `FITBIT_MCP_TZ`, `timezone` in config.json, the CLI's `config.toml` (Python 3.11+), or the Mac's zone.
* Other overrides: `FITBIT_MCP_GHEALTH_DIR`, `FITBIT_MCP_GHEALTH_CREDENTIALS`, `history_floor`
  (`FITBIT_MCP_HISTORY_FLOOR`) to never request data before a date.

How data is retrieved (ported from health-coach-app's `registry.py`, `api.py`, `ingest.py`, `backfill.py`):

| Rule | Detail |
| --- | --- |
| 23 live-verified types | sleep, exercise, daily RHR/HRV/SpO₂/respiratory rate/VO₂ max/skin temperature, respiratory-rate sleep summary, HRV and SpO₂ samples, heart rate, weight, active-zone minutes, activity level, sedentary periods, and daily roll-ups of steps, distance, floors, active energy, total calories, active minutes, time in HR zones |
| Filters | snake_case fields (`sleep.interval.civil_end_time`, `heart_rate.sample_time.physical_time`, `daily_resting_heart_rate.date` …); exclusive upper bound |
| Roll-ups | `POST …/dataPoints:dailyRollUp` (a read-only query) with ≤14-day ranges |
| Chunking / paging | per-type chunk sizes (3–90 days); page size 25 for sleep/exercise, else up to 10000; follow `nextPageToken` even after empty pages; repeated-token and 500-page guards |
| Heart rate | only over exercise sessions ±2 min (merged), unless `full_heart_rate=true` (~37 MB/day) |
| Full history | each type's first day is discovered by walking back one window at a time until 3 windows are empty, then reading the oldest window in full; cached; newest chunks first |
| Sync | re-fetches the last `overlap_days` (default 3) |
| Keys | session id; daily = date + source; sample = instant + source; interval = start, end, zone/level + source; roll-up = date |
| Values | `"NaN"` strings are missing, never numbers |
| Politeness | 0.2 s between requests; retries 408/429/5xx with back-off; one 401 refresh |

**All other documented types are imported too (by default).** Their time shape, fields and permission
come from Google's v4 API description (the copy the ghealth CLI caches, revision 2026-09-04) rather than a
live run: altitude, basal energy, swim lengths, calories in HR zones (roll-up), daily HR zones, VO₂ max,
run VO₂ max, body fat, height, core temperature, blood glucose, nutrition logs, hydration logs, ECG
(lower-bound filter only, upper bound applied locally), irregular-rhythm notifications, menstrual periods,
ovulation tests, symptoms and moods. Not imported: `food` and `food-measurement-unit`, which list Google's
shared food catalog (thousands of public foods), not your data; the foods you log arrive via nutrition logs. If the API rejects a type's primary
filter form, the importer tries the other documented form (e.g. `sample_time.civil_time`), remembers the one
that works, and reports a type only if none is accepted. Pass `include_unverified_types=false` to limit a
run to the 23 live-verified types.

**Permissions for symptoms, moods and reproductive health.** These need read-only scopes that the ghealth
CLI login usually doesn't include: `googlehealth.logged_symptoms.readonly` (symptoms),
`googlehealth.mindfulness.readonly` (moods) and `googlehealth.reproductive_health.readonly` (menstrual periods,
ovulation tests). Nutrition, ECG and IRN need `nutrition`, `ecg` and `irn` read-only scopes. To grant them:

1. In Google Cloud console, open the project that owns `~/.config/ghealth/client_secret.json` →
   **Google Auth Platform → Data Access → Add or remove scopes**, and add those `.readonly` scopes
   (keep your account listed as a test user while the app is in Testing).
2. In Claude Code: "Connect Google with the browser" → `connect_account(provider="google", method="browser")`.
   It requests every read-only data permission (not `location`) with incremental consent; approve them.
3. "Import all my history" (or "Import symptoms, moods and reproductive health").

Until a permission is granted, its types are recorded as `scope_not_granted` (never as empty), and
`connection_status` lists `not_granted` with these steps. The browser grant is stored in fitbit-mcp's own
token file; health-coach-app and the CLI are unaffected. In Testing mode it also expires after 7 days; if it
does, the server falls back to the CLI's credentials (with the CLI's smaller permission set) until you
connect with the browser again.

### Google Health API — a different approved project

Download a **Desktop app** OAuth client JSON to `~/.fitbit-mcp/client_secret.json` and call
`connect_account` with `method: "browser"` (PKCE, loopback `http://127.0.0.1:<port>/`). Google states it is
not onboarding new projects, so a new project will most likely not get access.

### Legacy Fitbit (existing app, until 2026-10-29)

1. At <https://dev.fitbit.com/apps> open your app. Prefer a **Personal** app with **Read Only** access.
2. Set the callback URL to exactly `http://127.0.0.1:8765/callback` (or change `fitbit_redirect_uri`).
3. Put the client ID in `~/.fitbit-mcp/config.json` → `fitbit_client_id`. A Personal app uses PKCE
   without a secret; a Server app needs `fitbit_client_secret` (and may lack intraday access).
   You can also use `FITBIT_CLIENT_ID` / `FITBIT_CLIENT_SECRET` environment variables.

### Takeout (no API needed)

Go to <https://takeout.google.com/>, deselect everything, select **Google Health** (or **Fitbit**),
create a ZIP export, download all parts into one folder.

## 4. Enable the server in Claude Code

Register the project-scoped server (this creates `.mcp.json`, or adds to an existing one without
touching other entries; it contains no secrets):

```sh
cd fitbit-mcp            # your clone
claude mcp add-json --scope project fitbit-local '{"type":"stdio","command":"uv","args":["run","--quiet","--frozen","--directory","${CLAUDE_PROJECT_DIR:-.}","fitbit-mcp"],"env":{"FITBIT_MCP_HOME":"${FITBIT_MCP_HOME:-~/.fitbit-mcp}"}}'
```

The resulting `.mcp.json` entry (you can also paste this into `.mcp.json` by hand):

```json
{
  "mcpServers": {
    "fitbit-local": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--quiet", "--frozen", "--directory", "${CLAUDE_PROJECT_DIR:-.}", "fitbit-mcp"],
      "env": {"FITBIT_MCP_HOME": "${FITBIT_MCP_HOME:-~/.fitbit-mcp}"}
    }
  }
}
```

1. Run `claude` in this folder and approve the project server **fitbit-local** when prompted
   (reset choices with `claude mcp reset-project-choices`).
2. Check with `claude mcp list` or `/mcp`; you should see 13 tools.
3. Leave `disconnect_account` requiring permission each time (don't add it to an allowlist).

If Claude Code can't find `uv` (e.g. launched from an app without your shell PATH), replace `"uv"`
with its absolute path from `which uv`. Without uv, point `command` at `.venv/bin/fitbit-mcp` created
by any Python ≥3.10 (`python3.12 -m venv .venv && .venv/bin/pip install -e .`).

## 5. Use it

Typical first session (say these in Claude Code):

* "Check my Fitbit connection status."
* "Connect my Google Health account." → reuses the ghealth CLI credentials (no browser).
  Then: "Is the connection job done?" — success is reported only after a verified read-only API call.
* "Import all my history." / "Import sleep and heart data from 2025-01-01 to 2026-10-06."
* "How's the import going?" · "Cancel that import." · "Resume job job_…"
* "Import my Takeout from ~/Downloads/takeout-20261007" (folder of parts or a single .zip/.tgz)
* "What data do I have, and what's missing or denied?"
* "Summarize my sleep by week for the last 3 months." · "What's the trend in my resting heart rate
  since June?" · "Average daily steps per month this year."
* "Show workouts longer than 45 minutes in September." · "Export my weight history as CSV."
* "Sync new data." (run daily/weekly) · "Disconnect Fitbit but keep my data."

## 6. Tools

| Tool | What it does |
| --- | --- |
| `connection_status` | Provider, configured?, token state, granted / not-granted permissions, last verification, optional live read-only check. Never returns secrets. |
| `connect_account` | Google: reuses the ghealth CLI credentials (or browser OAuth with PKCE); Fitbit: browser OAuth. Runs as a job; succeeds only after a verified API read. |
| `start_import` | Background import of all history (per-type origin discovery) or a date range; categories; Fitbit intraday; Google `full_heart_rate`, `include_unverified_types` (default true). |
| `sync_data` | Re-fetches an overlap window (default 3 days) since the last sync, upserts restated values, flags deletions. |
| `job_status` / `cancel_job` / `resume_job` | Non-blocking progress, cancellation (also during rate-limit waits), resume after cancel/failure/restart. |
| `list_data_types` | Metrics with counts, date coverage, units; denied/not-granted/failed categories; unparsed formats. |
| `query_data` | Filter by category, metric, provider, dates, granularity, value range; cursor pagination (≤500/page). |
| `summarize_data` | steps, distance, calories_out, sleep, resting_heart_rate, hrv, heart_rate, weight — with definition, coverage, exclusions, trend. |
| `export_data` | JSON/CSV of filtered records to `~/.fitbit-mcp/exports/` (owner-only file); returns the path. |
| `import_takeout` | Safe extraction + content-based format detection of Takeout/Fitbit exports. |
| `disconnect_account` | Removes local tokens; optional remote revocation and/or local deletion behind a two-step confirmation token. |

## 7. Data model and correctness

* **SQLite** (`fitbit.sqlite3`): one row per metric observation with provider, category, metric,
  granularity (sample/interval/session/daily), calendar `local_date`, civil start/end, UTC start/end,
  UTC offset, `time_basis`, value, unit, provider source IDs, data source, quality flags, revision,
  and links to the deduplicated original object and the retained raw response file.
* **Raw responses** are stored unmodified under `raw/<provider>/<group>/` with endpoint, parameters
  (never credentials), locale and SHA-256 in `raw_files`. Takeout files are kept where extracted.
* **Units are never mixed.** Fitbit responses use `Accept-Language: en_US` (miles, feet, pounds,
  fl oz), recorded per file; Google keeps documented units (g, mm, kcal, ms…). Summaries convert only
  known units; unknown units (e.g. Takeout weights) are excluded and counted.
* **Missing ≠ zero.** Absent values create no record. Fitbit series zeros are flagged
  `zero_may_mean_no_data`; zero-step days with a full sedentary day are treated as no-wear.
* **No double counting.** Each summary uses one provider (most coverage unless you choose), prefers
  daily totals over interval sums, sums intervals from one data source per day, and de-duplicates
  overlapping sleep sessions. Other providers with data are listed, not added.
* **Unsupported data is preserved and reported** (`list_data_types → unsupported_or_unparsed`).
  Google types without a verified field map use a generic parser that keeps the original object and
  infers units only from Google's documented field-name suffixes (e.g. `weightGrams`).
* **No arbitrary SQL**: inputs are validated (names, enums, strict dates) and every query is parameterized.

### Updates, deletions and their limits

* Records are keyed by stable provider identities (Google: session id, date + source, instant + source,
  interval + source, or roll-up date; Fitbit: `logId` or calendar date), so re-imports and syncs update
  in place (revision++) when the API restates recent days instead of duplicating.
* `sync_data` builds a **fresh request plan** for `[last synced date − overlap_days, today]`; it never
  reuses earlier responses (unlike rerunning the original exporter into the same folder).
* When a request covering a (type, date) scope completes, records the provider no longer returns are
  marked `deleted_upstream` (hidden by default, never erased). For Google this applies to civil/date-
  filtered types and roll-ups; physical-time types (samples, weight) and exercise-window heart rate are
  not reconciled, because their windows don't align with calendar days when you travel.
* Limits: edits or deletions older than the overlap window are not detected until a new full import;
  Fitbit workout/ECG/IRN lists are not reconciled for deletions; Google documents no "changed since"
  filter, so sync relies on the window; the sync watermark does not advance if a data request failed
  transiently (it does advance past permanent permission denials).

### Coverage notes

Fitbit: daily activity series, resting HR and HR zones, HRV, SpO2, breathing rate, skin temperature
(relative), VO2 max, sleep sessions/stages, weight/BMI/body fat logs, water and calories-in,
active-zone minutes, workouts (paginated), optional intraday heart rate (1 s) and minute activity.
Range endpoints replace the exporter's per-day requests (hundreds instead of tens of thousands of
requests for multi-year history, under the ~150 requests/hour quota). Snapshots (profile, devices,
goals, badges) are stored raw; ECG/IRN/temperature-core and TCX GPS files are not indexed.
Google: 42 documented types (23 live-verified + 19 from the API description; the food catalog is skipped), plus profile,
settings, IRN profile and paired devices (raw). Heart rate covers workouts unless `full_heart_rate`.
Takeout: Google-Health-API-shaped JSON and legacy Fitbit `steps-`, `calories-`, `heart_rate-`,
`resting_heart_rate-`, `sleep-`, `weight-YYYY-MM-DD.json` files (validated by structure). Their
timestamps carry no time zone, so they are stored as `unknown_zone`; CSV files are reported, not parsed.

## 8. Security

* Remote access is **read-only**: GET requests, plus Google's `dataPoints:dailyRollUp` aggregate query,
  which the API exposes as POST; the client refuses any other POST path, and no create/update/delete
  calls exist. Google scopes requested are `.readonly` only. OAuth uses PKCE (S256), a random `state`, and a callback bound to `127.0.0.1`.
* Tokens live in `~/.fitbit-mcp/credentials/` with owner-only permissions (not encrypted at rest —
  use FileVault). Refresh-token rotation is serialized by a thread lock plus an OS file lock and
  re-reads the token file inside the lock, so concurrent jobs/processes never reuse a spent token.
* Tool results and logs never contain access/refresh tokens, client secrets, authorization codes or
  full raw responses; log messages pass through a redaction filter. Diagnostics go to stderr and
  `~/.fitbit-mcp/logs/`; stdout carries only MCP messages (tested with deliberate stray output).
* Takeout extraction rejects absolute paths, `..`, drive letters, NUL bytes, symlinks/hard links and
  devices, and enforces per-file, total-size, file-count and compression-ratio limits (configurable).
* Revocation and local deletion require a single-use, argument-bound confirmation token that Claude
  must obtain first and may only use after you agree. Imported data is kept unless you ask to delete it.

## 9. Verification performed

Offline (`uv run pytest`, 76 tests, synthetic fixtures labeled as such): PKCE/state/callback
validation, forged-state rejection, declined consent, cancellation, refresh rotation under 8
concurrent threads, invalid refresh tokens, post-shutdown refusal, 401/429/5xx handling, cancellable
rate-limit waits, Fitbit and Google pagination (incl. host checks on `next` URLs), partial permissions
and provider denials, restart recovery after a simulated crash, cancel+resume without refetching,
de-duplication, sync updates and tombstones, inclusive date boundaries and leap days, Takeout traversal
/ symlink / bomb / size / count defenses and format detection, query validation and pagination,
export, and summary correctness. The Google fake rejects any filter field the registry doesn't name and
behaves like the live API as documented by health-coach-app (roll-up 14-day cap, empty first pages,
omitted empty days, nameless daily points); tests cover ghealth seeding without writing the CLI file,
self-healing re-auth, exercise-window heart rate, origin discovery, DST-correct physical filters and
`"NaN"` values. MCP: real stdio subprocess via the official client (initialize,
13 tools, representative calls), plus a raw JSON-RPC session (protocol 2025-06-18) proving stdout
purity. **Live:** full-history import and incremental syncs (23/23 request types) have been run
against a real Google Health account in October 2026. The legacy Fitbit route has only been tested
offline.

### Several servers, one database

Claude Code and the Claude desktop app each start their own `fitbit-mcp` process, and a client may
start two at once; all of them share `~/.fitbit-mcp`. Every job records the process that owns it and a
heartbeat refreshed by that process's live worker (every 15 s). If the owner stops (app quit, crash,
restart), any other running server, or the next one to start, notices within about 90 s, marks the job
interrupted and resumes it there, skipping completed requests (`FITBIT_MCP_AUTO_RESUME=0` turns the
automatic resume off). Starting a server never disturbs a job another live server is running, two
processes never run the same job, and `cancel_job` works whichever process receives it.

## 10. Troubleshooting

* `PERMISSION_DENIED` (Google): API not enabled, project not approved, scope not added, or not a test
  user. `ACCOUNT_NOT_LINKED`: sign into the mobile app with that Google account first.
* Redirect mismatch (Fitbit): the callback must exactly equal `fitbit_redirect_uri`.
* "Port in use": another program holds the Fitbit callback port; close it or change the URI.
* Google Testing-mode refresh tokens expire after 7 days → run `ghealth auth login`; the next request
  self-heals (or call `connect_account` again).
* Server not starting in Claude Code: run `uv sync`, then `claude --debug` or check
  `~/.fitbit-mcp/logs/server.log`.

## Reuse of the original exporter

Ported from `fitbit-data-exporter`: catalog (scopes, 42 Google types, endpoints), atomic private
writes, PKCE, callback/state validation, provider-host URL checks, error-code-only reporting,
Retry-After / Fitbit rate-limit parsing, 401-refresh-retry and pagination loop guards. Changed: all
`print`s removed; blocking sleeps became cancellable job waits; Google sign-in no longer uses
`google-auth-oauthlib`'s `run_local_server` (it prints to stdout and cannot be cancelled); the
whole-process lock became a refresh-only lock so jobs can run concurrently; the folder resume cache
became persisted per-job request plans plus deliberate refresh for sync.
