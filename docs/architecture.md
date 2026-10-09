# Architecture and extending

```text
 Google Health API / Fitbit API / Takeout
              │  read-only (GET + read-only roll-up queries, .readonly scopes)
              ▼
 ┌──────────────────────────┐     MCP (stdio)     ┌──────────────┐
 │ fitbit-local MCP server  │◀───────────────────▶│ Claude Code  │  "import my history",
 │ src/fitbit_mcp/          │                     │  + skills    │  "show me my stats"
 └────────────┬─────────────┘                     └──────┬───────┘
              │ writes                                   │ runs
              ▼                                          ▼
     ~/.fitbit-mcp/fitbit.sqlite3  ──── read-only ───▶  fitdash (.claude/skills/stats/scripts)
     + your logs (lifts, journal, notes)                 │
                                                         ├─ terminal dashboard
                                                         ├─ phone page (HTML)
                                                         ├─ morning notification
                                                         └─ coach notes ◀── claude -p (no tools)
```

## The MCP server: `src/fitbit_mcp/`

| Module | Role |
| --- | --- |
| `server.py` | MCP tools (13) and the app wiring; stdout is the MCP wire, so it never prints |
| `oauth.py`, `config.py` | PKCE loopback OAuth, token storage, settings from `~/.fitbit-mcp/config.json` |
| `client.py` | HTTP client: GET-only, retries, rate limits, pagination guards |
| `google_registry.py`, `catalog.py` | The data types, their filters, chunk sizes and permissions |
| `importer.py`, `jobs.py` | Background import/sync jobs: resumable, cancellable, restart-safe |
| `parsers/` | Provider payloads → normalized records |
| `store.py` | SQLite schema (`records`, `sessions`, `samples`, …) and parameterized writes |
| `queries.py`, `summaries.py` | Fixed, validated queries and summaries for the tools |
| `takeout.py` | Safe Takeout import (zip-bomb, symlink and size defenses) |

Details: [mcp-server.md](mcp-server.md).

## fitdash: `.claude/skills/stats/scripts/`

| Module | Role |
| --- | --- |
| `data.py` | Opens the DB read-only and builds the **model**: one JSON-serializable dict with every number (`fitdash --json`) |
| `scores.py` | Pure formulas, no I/O: recovery, strain, sleep, load, freshness, exercises, insights stats |
| `dashboard.py` | Renders the model with [rich](https://github.com/Textualize/rich), plus the CLI (`tag`, `lift`, `journal`, `coach`, `brief`, `web`) |
| `charts.py`, `muscle_icons.py` | Rings, sparklines, braille line charts, bars, half-block pixel icons |
| `lifts.py`, `journal.py` | The lift log and journal: parsing and storage |
| `coach.py` | When a note is due, the context sent to Claude, the rule-based fallback |
| `brief.py`, `web.py` | Notification, phone page, launchd agents |
| `tz.py` | Display timezone |
| `sync_now.py` | Runs one sync through the server's own code, for fitdash's auto-refresh |

Rules the code follows:

- **The model is the contract.** Renderers only format what `build_model()` computed, and the
  `--json` output is exactly what the boxes show.
- **No line overflows.** Tests render every section at 60–200 columns with `STATS_STRICT=1`, where
  any line wider than its box raises an error.
- **Every color cue has a glyph** (✓ ! ✕, ● ◐ ○, █ ▓ ▒ ░), so `--no-color` loses nothing. No
  terminal "dim" attribute, and secondary text keeps ≥ 4.5:1 contrast.

## Adding things

**A new metric in an existing box**

1. Read it in `data.build_model()` with `store.daily(...)` / `store.samples(...)`, using
   parameterized queries only.
2. Put any math in `scores.py` as a pure function with a test in `tests/test_scores.py`.
3. Add it to the model dict, then draw it in that box's `sec_*` function in `dashboard.py`.

**A new box**

1. Write `sec_mybox(m, w) -> list[Text]`: a headline, then a chart, then quiet details.
2. Register it in `PANELS`, add it to a column in `COLUMNS` and to `SECTIONS`.
3. The parametrized render tests cover it at every width automatically.

**A new exercise**: add it to `scores.EXERCISES` (primary and secondary muscles) and any aliases
to `EXERCISE_ALIASES`.

**A new habit**: no code. Add it to `"habits"` in `stats.json`.

## Tests

```sh
uv run pytest                                                                     # server
uv run --no-project --with rich --with pytest pytest .claude/skills/stats/tests   # fitdash
uv run --script docs/demo/make_demos.py                                           # refresh screenshots
```

Everything runs offline on synthetic data. The server tests use fakes of the Google and Fitbit
APIs that reject anything the real API would. fitdash's `fixture_db.py` builds a 70-day synthetic
history on the real schema. Tests never call Claude or the network.
