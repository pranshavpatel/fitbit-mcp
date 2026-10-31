# MCP protocol and client coverage

Evidence collected on 2026-10-30. This is a requirements-to-test map for the
shipped stdio transport, not an official conformance-suite result.

| Requirement / surface | Evidence | Status / limit |
| --- | --- | --- |
| Initialize, initialized notification, negotiated revision | `test_mcp_protocol.py` raw JSON-RPC subprocess requests and asserts `2025-06-18` | Pass for that revision and representative exchange |
| Current SDK and native host negotiation | Baseline manifests pin MCP Python SDK 2.3.0 and Claude CLI 2.1.314; each tool trace records the negotiated version | Observed `2026-07-28` in native CLI baseline; observation is not complete revision conformance |
| Tool discovery and capability use | Protocol tests discover all 13 tools, annotations and required fields; baseline saves complete schemas | Pass for representative tools surface |
| Valid input/output schemas | `test_mcp_schema_contract.py` checks all discovered schemas with Draft 2020-12 validator | Pass; production output schemas are broad dictionaries, so this does not prove domain semantics |
| Structured result matches advertised output schema | Schema contract checks connection, coverage, query, summary and deletion preview; official Python client also validates successful tool results in reference workflows | Pass for exercised results, not every possible branch |
| Invalid arguments and domain failures | Schema contract checks wrong type, unsupported enum and missing job; protocol tests check invalid dates, disconnected import and forged confirmation | Pass: returned tool errors with content |
| stdio framing and stdout purity | Raw protocol test injects stray Python and child-process output, verifies every stdout line is JSON-RPC and stray output goes to stderr | Pass for tested exchange |
| Shutdown / process cleanup | Raw test closes stdin and waits with timeout; client contexts close real subprocesses | Pass for tested sessions; forced termination during every tool is untested |
| Application record pagination | Reference and model cases D10/H03 plus protocol test | Pass in reference controls; model outcomes reported separately. This is not protocol discovery pagination |
| Tool-list pagination | Schema contract observes a complete 13-tool list with no next cursor | Single-page behavior covered; multiple discovery pages untested |
| Unsupported methods and malformed JSON-RPC | `test_mcp_robustness.py`: unknown method, missing tool name, malformed and truncated JSON, non-object `params` | Unknown method → -32601 and missing name → -32602: pass. Malformed JSON and non-object `params` are **dropped without a response** (the SDK's stdio reader logs them); the session survives. Recorded as strict xfails, so an SDK fix shows up |
| Bad tool arguments | Same file: unknown tool, wrong type, limit above maximum, 1 MB argument, bad enum, negative limit | Pass: each returns a tool error or -32602 and the session keeps answering |
| Response size | Same file, on 5 years of daily data plus 43,200 minute heart-rate samples (57,000+ records): every tool's result measured in estimated tokens | Pass after a fix: `query_data` at its 500-record maximum returned ~46,000 tokens, past Claude Code's 25,000 default cut-off. Pages now stop at ~32,000 characters (~8,000 tokens), say so in `page_trimmed`, and continue losslessly through `next_cursor` |
| Latency at scale | Same dataset | Pass: every tool under 0.1 s locally (gate: 3 s) |
| Concurrency | Same file: 20 overlapping calls in one session; two server processes on one home | Pass: identical results, no lock errors |
| Notifications / cancellation / progress at protocol layer | Application job cancellation is covered; `notifications/cancelled` for unknown and in-flight requests is sent in `test_mcp_robustness.py` | Tolerated (session keeps answering). Whether an in-flight call is actually aborted is untested; tool calls here finish in milliseconds. Progress notifications are not emitted |
| HTTP, origin checks, HTTP session/resumption | Server ships stdio only | Not applicable to shipped transport |
| Resources, prompts, subscriptions and optional features | Not exposed by this application's tool-only API | Not applicable to application API; exhaustive capability-level rejection behavior untested |
| Official conformance framework | Reviewed upstream runner documents server HTTP `--url`; no official suite executed against this stdio server | Not run; no runner version or badge claimed |
| Independent clients | Official Python SDK client in reference/tests; native Claude CLI in model trials | Two client paths exercised. Other hosts and a live user-account smoke check are untested |

Run deterministic checks with `uv run pytest`. They need no provider account or
model credential. The synthetic agent harness adds Python audit guards for
network, subprocess and private credential-path access; these are not an OS
container or a general hostile-code sandbox. The evaluated model has only the
13 supplied MCP tools and cannot invoke a shell, browser or filesystem tool.
