# Synthetic MCP tool-use evaluation

The model chooses real tools through a native Claude CLI MCP session. Each
trial runs a fresh production server/store over stdio, with deterministic fake
provider transport, synthetic credentials, fixed date and isolated local state.
No Fitbit/Google account or real health files are used.

From the repository root:

```sh
uv sync --frozen
uv run pytest
uv run python -m evals.mcp.run --mode reference --out /private/tmp/mcp-reference-new
uv run python -m evals.mcp.run --mode null --out /private/tmp/mcp-null-new
uv run python -m evals.mcp.run --mode agent --model haiku --profile portable --trials 3 --jobs 4 --out /private/tmp/mcp-portable-new
uv run python -m evals.mcp.run --mode agent --model haiku --profile configured --trials 3 --jobs 4 --out /private/tmp/mcp-configured-new
```

Outputs must be new directories. `--cases D01,D23` selects a pilot; `--split dev`
or `--split holdout` selects a split. Agent runs require an installed,
authenticated `claude` CLI supporting the flags pinned in the saved manifest.
There is no fallback to an unrestricted host if those flags fail. Normal PR
CI runs the synthetic reference/negative controls, not paid model calls.

Portable uses a generic data-assistant instruction; configured also supplies
repository AGENTS.md. Both receive the same production tools and server
instructions. The controller supplies predefined second user messages for
consent cases only after the first model turn and a real confirmation preview.
It never lets the model manufacture consent.

Implemented budgets: 30 tool calls, 600 seconds and $0.20 native CLI cost cap per
trial, with at most one scripted follow-up user message. There is no separate
64-decision-step limit. Up to 32 x 3 x $0.20 = $19.20 is the nominal per-profile
CLI cap, before provider-specific billing semantics; observed costs are saved.

`manifest.json` freezes inputs, dependency/CLI versions, instruction profile,
budgets and source hashes. `results.json` contains trial traces, state snapshots,
answers and deterministic scores. Per-trial folders also retain native model
events, discovered schemas, fake-provider requests and actual CSV artifacts.
Synthetic credential files are removed with the temporary trial directory.
Reference answers are controller-only; they are never supplied to the model.

The no-tool control is a scripted empty trace, not a second LLM baseline. It
should fail data-dependent tasks and can pass a refusal that requires no lookup.
Known-good controls are hand-authored Python MCP client plans, not agent scores.

See [plan](../../docs/mcp-tool-use-eval.md),
[protocol coverage](../../docs/mcp-protocol-coverage.md), and
[measured results](../../docs/mcp-tool-use-results.md). These are deterministic
pilot graders; narrative grounding and security are not human-calibrated, and
the reserved split is not fully blind after harness validation.
