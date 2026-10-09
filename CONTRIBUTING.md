# Contributing

Thanks for helping. Bug reports, new metrics, exercises, sections and docs fixes are all welcome.

## Setup

```sh
git clone https://github.com/pranshavpatel/fitbit-mcp.git && cd fitbit-mcp
uv sync
uv run pytest                                                                     # server tests
uv run --no-project --with rich --with pytest pytest .claude/skills/stats/tests   # fitdash tests
```

[docs/architecture.md](docs/architecture.md) explains where things live and how to add a metric or a box.

## Ground rules

- **Tests use synthetic data only.** Never commit real health data, exports, databases or tokens.
  `.gitignore` blocks the usual files; check `git status` anyway.
- **Read-only toward providers.** Remote calls stay GET (or read-only queries) with `.readonly`
  scopes. Nothing writes to a user's Google or Fitbit account.
- **Parameterized SQL only**, and no tool that runs free-form SQL.
- **The MCP server never prints to stdout**, because stdout is the protocol. Log with `fitbit_mcp.util.log`.
- **fitdash**: math goes in `scores.py` (pure, tested), data in `data.py` (the model), and drawing
  in `dashboard.py`. New boxes must pass the strict width tests at 60–200 columns. Keep every color
  cue paired with a glyph.
- If you change how a screen looks, regenerate the demos: `uv run --script docs/demo/make_demos.py`.

## Pull requests

Keep them focused. Describe what changed and why, and make sure both test suites pass. CI runs
them on macOS and Linux.
