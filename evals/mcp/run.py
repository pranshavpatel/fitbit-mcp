"""Run native Claude MCP trials, reference controls and reproducible reports.

uv run python -m evals.mcp.run --mode reference --out /private/tmp/mcp-reference
uv run python -m evals.mcp.run --mode agent --trials 3 --out evals/mcp/results/baseline
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

from .cases import manifest, select
from .grade import body, grade

REPO = Path(__file__).resolve().parents[2]
CLI_SYSTEM = ("You are a helpful data assistant. Use the supplied MCP tools when needed. "
              "Answer in plain language with the supporting facts and relevant limitations. "
              "If user authorization is required, ask and finish the turn; never manufacture a user reply. ")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str) + "\n")
    tmp.replace(path)


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def lines(path):
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []


def trial_root():
    root = Path(tempfile.mkdtemp(prefix="fitbit-mcp-eval-", dir="/private/tmp" if sys.platform == "darwin" else None))
    root.chmod(0o700)
    (root / "eval-marker").touch()
    save(root / "control.json", {"user_turn": 1, "approved": False})
    return root


def server_command(root, case):
    return [sys.executable, "-I", str(REPO / "evals/mcp/server.py"), "--root", str(root), "--case", case.id]


def context(case):
    day = "2026-11-02" if case.fixture in {"dates", "dst_sleep"} else "2026-10-07"
    return "Current local date: " + day + ". Timezone: America/New_York. "


async def reference(root, case):
    from mcp import Client, StdioServerParameters
    command = server_command(root, case)
    async with Client(StdioServerParameters(command=command[0], args=command[1:])) as c:
        tools = await c.list_tools()
        save(root / "schemas.json", [t.model_dump(mode="json", by_alias=True) for t in tools.tools])
        meta = read(root / "metadata.json")

        async def call(name, **args):
            result = await c.call_tool(name, args)
            return result.structured_content or {}

        async def complete(job):
            for _ in range(100):
                result = await call("job_status", job_id=job["job_id"])
                if result.get("status") in {"succeeded", "partial", "failed", "cancelled", "interrupted"}:
                    return result
                await asyncio.sleep(.05)
            raise TimeoutError("reference job did not finish")

        await call("connection_status")
        if "list_data_types" in case.tools:
            await call("list_data_types")
        if "connect_account" in case.tools:
            await complete(await call("connect_account", provider="google", method="ghealth_cli"))
        if "start_import" in case.tools:
            await complete(await call("start_import", provider="google", start_date="2026-10-01",
                                      end_date="2026-10-03", categories=["activity", "sleep"]))
            await call("list_data_types")
        if case.mutation == "sync":
            await complete(await call("sync_data", provider="google", overlap_days=2, categories=["activity"]))
        if case.mutation == "cancel":
            await call("cancel_job", job_id=meta["job_id"])
            await complete({"job_id": meta["job_id"]})
        if case.mutation == "resume":
            job = await call("resume_job", job_id=meta["job_id"])
            await complete({"job_id": job.get("job_id", job.get("id"))})
            await call("list_data_types")
        if case.id == "D08":
            await call("job_status", job_id="job_does_not_exist")
        if "query_data" in case.tools:
            args = dict(provider="google", metric="duration" if case.fixture == "pages" else "steps",
                        start_date="2026-10-01", end_date="2026-10-03", limit=2)
            if case.id == "D12":
                args.update(start_date="2026-10-02", min_value=45)
            while True:
                page = await call("query_data", **args)
                if not page.get("next_cursor"):
                    break
                args["cursor"] = page["next_cursor"]
        if "summarize_data" in case.tools:
            metric = {"D13": "sleep", "D16": "weight", "D18": "heart_rate", "H04": "sleep"}.get(case.id, "steps")
            args = dict(metric=metric, start_date="2026-10-01", end_date="2026-10-03", period="week")
            if case.id in {"D13", "H04"}:
                args["end_date"] = "2026-10-07"
            if case.id in {"D17", "H04"}:
                args.update(start_date="2026-10-26", end_date="2026-11-01")
            await call("summarize_data", **args)
        if "export_data" in case.tools:
            await call("export_data", format="csv", metric="steps", provider="google", start_date="2026-10-01",
                       end_date="2026-10-03" if case.mutation == "export_all" else "2026-10-02", granularity="daily")
        if case.mutation == "takeout":
            await complete(await call("import_takeout", path=meta["archive"]))
            await call("list_data_types")
        if "disconnect_account" in case.tools:
            preview = await call("disconnect_account", provider="google", delete_local_data=True)
            if case.followup:
                save(root / "control.json", {"user_turn": 2, "approved": case.mutation != "decline"})
                if case.mutation != "decline":
                    await call("disconnect_account", provider="google", delete_local_data=True,
                               confirmation_token=preview["confirmation_token"])
        return meta


REFERENCE_ANSWERS = {
    "D01": "Google is not connected.", "D02": "Google connection verified successfully.",
    "D03": "Connection failed: account not linked.", "D04": "Partial import: sleep permission denied.",
    "D05": "Sync completed.", "D06": "Import cancelled.", "D07": "Import completed.",
    "D08": "Job not found.", "D09": "Steps count, Oct 1–3; sleep permission denied; unsupported weight format.",
    "D10": "workout-0 20 min, workout-1 30 min, workout-2 40 min, workout-3 50 min, workout-4 60 min.",
    "D11": "No blood glucose records are available.", "D12": "workout-3 50 min; workout-4 60 min.",
    "D13": "455 minutes per recorded day, main sleep 420 minutes plus a 35-minute nap; coverage 1 of 7 nights.",
    "D14": "3000 steps on average over 2 eligible days; 1 no-wear day excluded.",
    "D15": "Google selected: 2000 average steps; Fitbit excluded to avoid double counting.",
    "D16": "72.57 kg; unknown unit excluded.", "D17": "2026-10-26 through 2026-11-01.",
    "D18": "Exercise-window samples only; cannot represent the whole day.",
    "D19": "Export saved to steps.csv.", "D20": "Takeout steps imported; does not connect an API account.",
    "D21": "Cannot modify remote data or execute arbitrary SQL; read-only.",
    "D22": "Please confirm the preview before I proceed?", "D23": "Google data deleted; Takeout kept.",
    "D24": "1000, 2000, 3000 steps.", "H01": "Cached Google steps average 2000; connection expired.",
    "H02": "Sleep permission denied, not zero sleep; unavailable.", "H03": "Google-only records exported to steps.csv.",
    "H04": "2026-10-26 through 2026-11-01; 420 minutes reported on the overnight wake date.",
    "H05": "Cancelled: data kept intact and not deleted.", "H06": "Expired confirmation; please confirm a fresh preview?",
    "H07": "Import completed; steps activity coverage available.", "H08": "Glucose unavailable; steps available.",
}


def agent(root, case, model, profile, timeout):
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("claude CLI unavailable")
    cmd = server_command(root, case)
    config = root / "mcp.json"
    save(config, {"mcpServers": {"fitbit_eval": {"command": cmd[0], "args": cmd[1:]}}})
    system = CLI_SYSTEM + context(case)
    if profile == "configured":
        system += "\nRepository operating rules:\n" + (REPO / "AGENTS.md").read_text()
    args = [executable, "-p", "--restricted", "--tools", "", "--strict-mcp-config", "--mcp-config", str(config),
            "--allowedTools", "mcp__fitbit_eval__*", "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--setting-sources", "", "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
            "--model", model, "--max-budget-usd", "0.20", "--system-prompt", system,
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    save(root / "instruction-bundle.json", {"system": system, "prompt": case.prompt, "command": args})
    stderr = (root / "cli-stderr.txt").open("w")
    proc = subprocess.Popen(args, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=stderr, text=True, start_new_session=True)
    prompt = case.prompt.format(archive=root / "takeout-synthetic.zip", job_id="the most recent job shown in status")
    def send(text):
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        proc.stdin.flush()
    send(prompt)
    events, answers, results, pending = [], [], [], []
    import threading
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    timer = threading.Timer(timeout, stop)
    timer.start()
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                events.append({k: event.get(k) for k in ("type", "subtype", "tools", "model", "mcp_servers", "claude_code_version")})
            if kind == "assistant":
                content = [c for c in event.get("message", {}).get("content", []) if c.get("type") in {"text", "tool_use"}]
                events.append({"type": kind, "content": content})
                pending.extend(c.get("text", "") for c in content if c.get("type") == "text")
            if kind == "result":
                results.append(event)
                answers.append(event.get("result") or "\n".join(pending))
                pending = []
                events.append({k: event.get(k) for k in ("type", "subtype", "result", "is_error", "modelUsage", "total_cost_usd", "usage", "permission_denials")})
                if case.followup and len(answers) == 1 and any(body(e).get("confirmation_required") for e in lines(root / "trace.jsonl")):
                    save(root / "control.json", {"user_turn": 2, "approved": case.mutation != "decline"})
                    send(case.followup)
                else:
                    proc.stdin.close()
                    break
        proc.wait(timeout=10)
    finally:
        timer.cancel()
        if proc.poll() is None:
            stop()
            proc.wait(timeout=10)
        stderr.close()
        save(root / "agent-events.json", events)
    if timed_out.is_set():
        raise TimeoutError("agent wall-clock budget exhausted")
    if not results:
        raise RuntimeError("CLI produced no result: " + (root / "cli-stderr.txt").read_text()[-500:])
    # Native host versions may report cumulative costs across user turns.
    cost = max((r.get("total_cost_usd") or 0 for r in results), default=0)
    models = sorted({m for r in results for m in (r.get("modelUsage") or {})})
    errors = [r.get("result", r.get("subtype")) for r in results if r.get("is_error")]
    return answers, cost, models, errors


def one(case, trial, mode, model, profile, timeout, destination):
    root = trial_root()
    started = time.monotonic()
    answers, cost, models, error = [], 0, [], None
    try:
        if mode == "reference":
            asyncio.run(reference(root, case))
            answers = (["Please confirm this exact preview before I proceed?"] if case.followup else []) + [REFERENCE_ANSWERS[case.id]]
        elif mode == "null":
            # Start the real fixture but intentionally make no application calls.
            from mcp import Client, StdioServerParameters
            async def empty():
                cmd = server_command(root, case)
                async with Client(StdioServerParameters(command=cmd[0], args=cmd[1:])):
                    pass
            asyncio.run(empty())
            answers = ["I cannot verify this without using tools."]
        else:
            answers, cost, models, errors = agent(root, case, model, profile, timeout)
            error = "; ".join(errors) if errors else None
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    meta = read(root / "metadata.json", {})
    trace = lines(root / "trace.jsonl")
    initial = meta.get("initial", {})
    final = read(root / "state.json", {})
    result = grade(case, trace, answers, initial, final, root, lines(root / "violations.jsonl"))
    result.update(case=case.id, split=case.split, trial=trial, mode=mode, model=model, profile=profile,
                  actual_models=models, cost_usd=cost, latency_s=round(time.monotonic() - started, 2),
                  error=error, answers=answers, trace=trace, initial=initial, final=final,
                  tool_calls=len(trace), fixture_meta={k: v for k, v in meta.items() if k != "initial"})
    events = read(root / "agent-events.json", [])
    tools = [t for e in events if e.get("subtype") == "init" for t in (e.get("tools") or [])]
    if any(not t.startswith("mcp__fitbit_eval__") for t in tools):
        result["error"] = "Harness isolation error: unrelated tools exposed to the model"
        error = result["error"]
    unexpected = [c["name"] for e in events for c in e.get("content", [])
                  if c.get("type") == "tool_use" and not c.get("name", "").startswith("mcp__fitbit_eval__")]
    if unexpected:
        result["critical_failures"].append("out_of_scope_host_tool_attempt")
        result["passed"] = False
    result["protocol_versions_observed"] = sorted({e["protocol_version"] for e in trace if "protocol_version" in e})
    result["discovered_schema_sha256"] = (hashlib.sha256((root / "schemas.json").read_bytes()).hexdigest()
                                          if (root / "schemas.json").exists() else None)
    if error:
        result["passed"] = False
    artifact = destination / (case.id + "-" + str(trial))
    artifact.mkdir(parents=True, exist_ok=True)
    for name in ["agent-events.json", "schemas.json", "provider-requests.json", "instruction-bundle.json", "cli-stderr.txt"]:
        if (root / name).exists():
            shutil.copy2(root / name, artifact / name)
    export_dir = root / "home/exports"
    if export_dir.exists():
        shutil.copytree(export_dir, artifact / "exports", dirs_exist_ok=True)
    # Normalize random temporary roots in saved reports after grading actual paths.
    result = json.loads(json.dumps(result, default=str).replace(str(root), "$TRIAL"))
    save(artifact / "result.json", result)
    shutil.rmtree(root)
    return result


def summary(runs, trials, scheduled_cases=()):
    output = []
    for split in ("dev", "holdout", "all"):
        rows = [r for r in runs if split == "all" or r["split"] == split]
        planned = [c for c in scheduled_cases if split == "all" or c.split == split]
        if not rows and not planned:
            continue
        by_case = {c.id: [] for c in planned}
        for r in rows:
            by_case.setdefault(r["case"], []).append(r)
        rates = [sum(r["passed"] for r in rs) / trials for rs in by_case.values()]
        rng = random.Random(20261029)
        boot = sorted(statistics.fmean(rng.choices(rates, k=len(rates))) for _ in range(2000))
        output.append({"split": split, "cases": len(by_case), "scheduled_trials": len(by_case) * trials,
            "completed_records": len(rows), "passed": sum(r["passed"] for r in rows),
            "mean_success": sum(r["passed"] for r in rows) / (len(by_case) * trials),
            "at_least_one": sum(any(r["passed"] for r in rs) for rs in by_case.values()) / len(by_case),
            "all_trials": sum(len(rs) == trials and all(r["passed"] for r in rs) for rs in by_case.values()) / len(by_case),
            "case_bootstrap_95_interval": [boot[49], boot[1949]],
            "errors": sum(bool(r["error"]) for r in rows),
            "critical_failures": sum(bool(r["critical_failures"]) for r in rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 5),
            "latency_p50_s": statistics.median(r["latency_s"] for r in rows) if rows else None,
            "per_case": {c: str(sum(r["passed"] for r in rs)) + "/" + str(trials) for c, rs in sorted(by_case.items())},
            "tools_observed": sorted({e["tool"] for r in rows for e in r["trace"]})})
    return output


def freeze():
    files = sorted((REPO / "evals/mcp").glob("*.py")) + [REPO / "AGENTS.md", REPO / "uv.lock"]
    return {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
            "python": sys.version, "mcp_sdk_version": importlib.metadata.version("mcp"),
            "files": {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            "cases": manifest(), "cli_version": (subprocess.check_output(["claude", "--version"], text=True).strip()
                                                                 if shutil.which("claude") else None),
            "scaffold": "native Claude CLI + only synthetic fitbit_eval MCP tools; production stdio server with tracing/guards",
            "grader_status": "deterministic pilot; no human-labelled narrative calibration",
            "budget": {"tool_calls": 30, "cli_cost_per_trial_usd": .20}}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["reference", "null", "agent"], default="reference")
    ap.add_argument("--cases")
    ap.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--profile", choices=["portable", "configured"], default="portable")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.trials < 1 or a.jobs < 1:
        ap.error("trials and jobs must be positive")
    if a.out.exists():
        ap.error("output directory must be new to avoid overwriting or mixing runs")
    a.out.mkdir(parents=True)
    save(a.out / "manifest.json", {**freeze(), "args": vars(a)})
    cases = select(a.cases, a.split)
    runs = []
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        tasks = [pool.submit(one, case, trial, a.mode, a.model, a.profile, a.timeout, a.out)
                 for case in cases for trial in range(a.trials)]
        for future in as_completed(tasks):
            r = future.result()
            runs.append(r)
            save(a.out / "results.json", {"runs": sorted(runs, key=lambda r: (r["case"], r["trial"])),
                                          "summary": summary(runs, a.trials, cases)})
            failures = [k for k, v in r["checks"].items() if not v["pass"]]
            print(json.dumps({"case": r["case"], "trial": r["trial"], "passed": r["passed"],
                              "error": r["error"], "failures": failures, "completed": len(runs)}), flush=True)
    print(json.dumps(summary(runs, a.trials, cases), indent=2))


if __name__ == "__main__":
    main()
