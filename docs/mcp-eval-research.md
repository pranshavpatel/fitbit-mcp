# MCP eval research and plan review

Reviewed **2026-10-29** against primary sources and this repository's code,
tests, CI and [planned tool-use eval](mcp-tool-use-eval.md).

## Assessment

**The original plan was a sound application-specific agent-eval design, but
incomplete as a broader MCP evaluation plan.** The reviewed sources do not
establish a universal agent-eval certification, mandatory case count, or 90%
passing score. MCP protocol conformance is specified; agent task quality is
measured using differing benchmark methodologies. “Industry-aligned” is our
assessment of the design, not an official status.

The updated plan incorporates the gaps below. An initial runner and synthetic
baseline are now [implemented and measured](mcp-tool-use-results.md). Broader
coverage and calibration remain planned; no official conformance result is claimed.

## What the sources establish

| Primary source | What it contributes | Authority / limit |
| --- | --- | --- |
| [MCP conformance framework](https://github.com/modelcontextprotocol/conformance) | Versioned wire/schema and behavioral checks; explicit requirement sets and known-failure reporting | Official protocol implementation testing, not LLM task-quality certification. Reviewed server CLI targets HTTP URLs; direct applicability to our stdio server needs mapping. |
| [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle) and [tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) | Negotiation/capabilities, schemas, separate protocol/tool errors, and trust boundaries | Normative for that revision. Do not assume a different revision has the same lifecycle or requirements. |
| [MCP-Bench paper](https://arxiv.org/abs/2508.20453) and [authors' repository](https://github.com/Accenture/mcp-bench) | Multi-step tool discovery, argument control, planning and task completion; single/multi-server settings | Research benchmark. Its mixed rule/judge composite is not a universal grading formula, and its scores do not transfer to Fitbit tasks. |
| [MCPMark, ICLR 2026](https://proceedings.iclr.cc/paper_files/paper/2026/hash/8138d211ce8790fdfbeeeb9781838a37-Abstract-Conference.html) and [evaluation documentation](https://github.com/eval-sys/mcpmark/blob/main/docs/introduction.md) | Curated initial states, programmatic verification and repeated-run metrics | Research benchmark with 127 tasks; that scale is not a minimum requirement for a new domain-specific suite. |
| [τ-bench 2 evaluation semantics](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md) | Equivalent final states can pass without matching one reference action sequence; required communication is a separate component | Tool-agent methodology beyond MCP. Read-only tasks still need answer checks because “no state change” can otherwise pass trivially. |
| [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) | Isolated environments, reference solutions, balanced cases, calibrated graders and reliability distinctions | Vendor engineering guidance, not a protocol standard. It recommends starting small rather than waiting for hundreds of cases. |
| [Anthropic: Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents) | Realistic multi-step tasks, raw trace inspection, held-back cases and tool/token/error/latency metrics | Vendor guidance directly relevant to MCP tool ergonomics; it does not mandate one host or evaluation framework. |
| [MCP-SafetyBench, ICLR 2026](https://proceedings.iclr.cc/paper_files/paper/2026/hash/d46f127a80dc58cbc0732a717285c43a-Abstract-Conference.html) | Multi-turn attacks spanning server, host and user surfaces; evaluate the safety/utility tradeoff | Security benchmark, not a complete threat model or certification for our server. One embedded-instruction test is narrower coverage. |
| [Official Python SDK testing guide](https://py.sdk.modelcontextprotocol.io/get-started/testing/) | Production SDK client supports isolated server testing, including in-memory tests | Useful implementation technique; an in-memory tool test alone does not exercise stdio or model-chosen workflows. |

Sources were read on the review date. GitHub `main` and web documentation can
change; implementation must save exact upstream revisions and runner versions.
Similarly named **MCPBench**, **MCP-Bench**, **MCPMark** and the official
conformance suite are separate projects, not interchangeable labels.

## Gap assessment of the original plan

| Area | Before review | Decision added to the plan |
| --- | --- | --- |
| Real model chooses real MCP calls | Already planned | Retain production stdio/server/store path; no canned successful tool outputs |
| Deterministic state and artifact oracles | Already planned | Require a passing reference and failing counterexample for every case; require facts for read-only answers |
| Equivalent valid workflows | Already planned | Retain outcome grading; enforce only genuine instruction/authorization dependencies |
| Async failures, missing coverage, pagination and consent | Strong application coverage | Retain all 32 initial cases; separate app cursors from protocol pagination |
| Protocol conformance | Only representative integration coverage | Add revision/capability requirements mapping and explicit pass/fail/untested/N/A evidence |
| Model/host dependence | One unspecified model/scaffold | Separate portable/configured instructions, model families, independent clients and actual-host smoke coverage |
| Repetition and uncertainty | Three trials and spread, without named reliability metrics | Add explicit mean success, empirical pass@3 and pass^3; case-level uncertainty and estimator disclosure |
| Injection and adversarial breadth | Two closely related embedded-instruction cases | Add source/error/delayed/canary variants; separate hostile-companion host profile |
| Balanced tool selection | Disabled-tools control, no explicit overuse cases | Add conceptual/no-lookup and ambiguity cases, plus irrelevant-record and paraphrase variants |
| Evaluation realism | Synthetic fixtures, potential instruction advantage | Ground fixtures in real workflows rewritten synthetically; separate repository hints from portable tool usability |
| Runtime boundaries | Global network denial and ambiguous turn budget | Isolate server egress from allowed model inference; distinguish user messages from model/tool steps |
| Continuous regression | Not operationalized | Specify deterministic PR checks and cost-bounded scheduled/release agent runs |
| Release claims | Proposed 90% gate, no external basis | Label it internal; keep conformance and critical failures separate; no certification claim |

The revised plan is reasonably aligned with these published practices. The
initial run supplies execution evidence; narrative grader calibration and the
broader coverage profiles are still missing.
No numerical “alignment score” is assigned: it would create precision without
a recognized scoring rubric.

## Scope choices that remain appropriate

- **Keep 32 initial cases.** A domain-specific pilot can be useful at this size;
  add independent failure modes before making narrow confidence claims.
- **Keep synthetic provider fixtures.** They make OAuth, denied permissions,
  job transitions and destructive previews reproducible without real accounts.
  Call this a controlled end-to-end system test, not a live-provider benchmark.
- **Keep the read-only product scope.** Public benchmarks' CRUD breadth does
  not justify adding remote writes to a health-data MCP. Evaluate allowed local
  imports/exports and deny unsupported remote mutations.
- **Do not install a large benchmark stack just for its name.** Our tasks need
  project-specific fixtures and oracles; a small runner can implement the useful
  methodology. Reusing infrastructure is optional, subject to reproducibility.

## Evidence in this repository

- `uv.lock` selects MCP Python SDK 2.3.0; `pyproject.toml` permits `>=2.3,<3`.
- `tests/test_mcp_protocol.py` exercises initialization/discovery, a synthetic
  import followed by job polling, queries/pages, a summary, export, error cases,
  confirmation binding, deletion isolation and stdout purity. Its raw client
  explicitly asserts `2025-06-18`; that is not all-revision conformance evidence.
- `.github/workflows/tests.yml` runs server/Fitdash tests plus lint/type checks.
  It does not currently run a model-driven MCP eval or official conformance job.
- The coach result files cover prepared dashboard insights and workout reviews,
  not arbitrary natural-language requests mediated through the 13 MCP tools.

Execution follow-up (2026-10-30): the runner, cases, manifests, trace/state/file
graders and baseline artifacts are implemented. Read the
[results](mcp-tool-use-results.md) and [protocol map](mcp-protocol-coverage.md)
for exact evidence and remaining gaps. The broader plan is not fully completed.
