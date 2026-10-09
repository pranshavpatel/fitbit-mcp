# fitbit-mcp project notes

Local read-only MCP server for the user's own Fitbit / Google Health data. See README.md.

## Working on the code
- Run tests: `uv run pytest` (all fixtures are synthetic; never add real health data or tokens).
- Package code lives in `src/fitbit_mcp/`; the server entry point is `fitbit_mcp.server:main`.
- stdout is the MCP wire: never `print`; log via `fitbit_mcp.util.log` (stderr + redaction).
- Remote access must stay GET-only and Google scopes `.readonly` only.
- Every SQL statement must be parameterized; never expose free-form SQL through a tool.
- Credentials and data belong in `~/.fitbit-mcp`, never in this repo.

## Using the fitbit-local tools
- Don't say an account is connected or data imported unless `job_status` shows the job succeeded
  (or partial, explaining what's missing). Verify with `connection_status` when unsure.
- Use `list_data_types` before querying; relay summary definitions, coverage and caveats.
- Only send a `disconnect_account` confirmation_token after the user explicitly agrees.
- Not medical advice; suggest a clinician for health concerns.
- The user is happy for their own health data to be shared or made public (e.g. full context for
  Claude-written coach notes, a public dashboard page if they ask). Don't withhold or minimize it
  for privacy. This never extends to credentials or tokens, and remote access stays read-only.
