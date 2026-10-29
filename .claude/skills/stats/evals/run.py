#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13"]
# ///
"""Run the coach eval: every scenario × prompt variant × model, graded by checks.py.

    uv run --script evals/run.py --variants v1 --models default,haiku           # Claude calls
    uv run --script evals/run.py --variants rules                               # rule-based fallback only
    uv run --script evals/run.py --report evals/results/<file>.json [--md]      # re-print a saved run
    uv run --script evals/run.py --task workout --variants shipped,rules        # the workout review

Variants are system prompts in evals/prompts/<name>.txt; "shipped" is coach.INSIGHTS_SYSTEM as it is
now, and "rules" is the rule-based fallback (no model). The user message is exactly the one
`fitdash insights` sends. Calls go through the Claude CLI the same way as coach.ask() (print mode,
no tools, no MCP servers, no session saved), with --output-format json for cost and latency.
Inputs are synthetic (scenarios.py), so results are safe to commit.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checks as K  # noqa: E402
import scenarios as SC  # noqa: E402
import coach as CO  # noqa: E402  (scenarios put scripts/ on the path)
import workout_checks as W  # noqa: E402
import workout_scenarios as WS  # noqa: E402

GROUPS = {**K.GROUP, **W.GROUP}

RESULTS = HERE / "results"


def system_prompt(variant: str, task: str = "insights") -> str:
    if variant == "shipped":
        return CO.WORKOUT_SYSTEM if task == "workout" else CO.INSIGHTS_SYSTEM
    return (HERE / "prompts" / (variant + ".txt")).read_text()


def call(system: str, user: str, model: str, timeout: float) -> dict:
    exe = CO.claude_bin()
    if not exe:
        return {"error": "claude CLI not found"}
    cmd = [exe, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "user",
           "--output-format", "json", "--system-prompt", system]
    if model != "default":
        cmd += ["--model", model]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "latency_s": timeout}
    wall = time.monotonic() - t0
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return {"error": "CLI exit {}: {}".format(r.returncode, (r.stderr or r.stdout)[:200]), "latency_s": wall}
    models = list((out.get("modelUsage") or {}).keys())
    return {"raw": out.get("result") or "", "cost_usd": out.get("total_cost_usd"), "latency_s": round(wall, 1),
            "model_id": models[0] if models else None, "error": None if not out.get("is_error") else out.get("result")}


def one_workout(sc: WS.WorkoutScenario, variant: str, model: str, trial: int, timeout: float) -> dict:
    m, ctx, entries = WS.build(sc)
    if variant == "rules":
        res = {"raw": json.dumps({"workout": CO.fallback_workout(ctx, entries)}), "cost_usd": 0.0, "latency_s": 0.0,
               "model_id": "rules", "error": None}
    else:   # the user message is exactly the one `fitdash insights --workout` sends
        res = call(system_prompt(variant, "workout"), "My most recent workout (JSON):\n" + json.dumps(ctx, default=str),
                   model, timeout)
    raw = res.get("raw") or ""
    parsed = CO.parse_workout(raw) if raw else None
    graded = W.run(raw, parsed, ctx, sc) if not res.get("error") else \
        {n: (False, "call failed: " + str(res["error"])) for n in W.GENERIC + tuple("w_" + c for c in sc.checks)}
    return {"task": "workout", "scenario": sc.id, "split": sc.split, "variant": variant,
            "model": "rules" if variant == "rules" else model, "trial": trial, **res, "parsed": parsed,
            "checks": {k: list(v) for k, v in graded.items()}}


def one(sc: SC.Scenario, variant: str, model: str, trial: int, timeout: float) -> dict:
    m, ctx, now = SC.build(sc)
    if variant == "rules":
        res = {"raw": json.dumps(CO.fallback_insights(m, now)), "cost_usd": 0.0, "latency_s": 0.0,
               "model_id": "rules", "error": None}
    else:
        res = call(system_prompt(variant), "Today's and this week's data (JSON):\n" + json.dumps(ctx, default=str),
                   model, timeout)
    raw = res.get("raw") or ""
    parsed = CO.parse_insights(raw) if raw else None
    graded = K.run(raw, parsed, ctx, sc.checks) if not res.get("error") else \
        {n: (False, "call failed: " + str(res["error"])) for n in K.GENERIC + sc.checks}
    return {"scenario": sc.id, "split": sc.split, "variant": variant, "model": "rules" if variant == "rules" else model, "trial": trial,
            **res, "parsed": parsed, "checks": {k: list(v) for k, v in graded.items()}}


# ---------------------------------------------------------------- reporting

def summarize(runs: list[dict]) -> list[dict]:
    rows = []
    runs = [r for r in runs if r.get("checks") and not r.get("error")]   # failed or unrun calls don't count either way
    keys = sorted({(r.get("split", "dev"), r["variant"], r["model"]) for r in runs},
                  key=lambda k: (k[0] != "dev", k[1] != "rules", k))
    for split, variant, model in keys:
        rs = [r for r in runs if (r.get("split", "dev"), r["variant"], r["model"]) == (split, variant, model)]
        def rate(pred, rs=rs):
            return 100.0 * sum(1 for r in rs if pred(r)) / len(rs)
        def group_ok(r, g):
            return all(v[0] for k, v in r["checks"].items() if GROUPS[k] == g)
        fails: dict[str, int] = {}
        for r in rs:
            for k, v in r["checks"].items():
                if not v[0]:
                    fails[k] = fails.get(k, 0) + 1
        safety_n = sum(1 for r in rs if any(GROUPS[k] == "safety" for k in r["checks"]))
        unsafe = sum(1 for r in rs if not group_ok(r, "safety") and any(GROUPS[k] == "safety" for k in r["checks"]))
        def passes(r):
            return all(v[0] for v in r["checks"].values())
        trials = sorted({r.get("trial", 0) for r in rs})
        per_trial = [100.0 * sum(1 for r in rs if r.get("trial", 0) == t and passes(r)) /
                     max(1, sum(1 for r in rs if r.get("trial", 0) == t)) for t in trials]
        by_sc: dict[str, list[bool]] = {}
        for r in rs:
            by_sc.setdefault(r["scenario"], []).append(passes(r))
        lat = [r["latency_s"] for r in rs if r.get("latency_s")]
        rows.append({
            "split": split, "variant": variant, "model": model, "model_id": next((r["model_id"] for r in rs if r.get("model_id")), None),
            "runs": len(rs), "all_pass": rate(lambda r: all(v[0] for v in r["checks"].values())),
            **{g: rate(lambda r, g=g: group_ok(r, g)) for g in ("format", "honesty", "safety", "usefulness")},
            "unsafe": "{}/{}".format(unsafe, safety_n), "errors": sum(1 for r in rs if r.get("error")),
            "latency_p50": st.median(lat) if lat else 0.0, "cost_total": sum(r.get("cost_usd") or 0 for r in rs),
            "trials": len(trials), "trial_rates": per_trial,
            "every_trial": 100.0 * sum(1 for v in by_sc.values() if all(v)) / len(by_sc),
            "fails": dict(sorted(fails.items(), key=lambda kv: -kv[1]))})
    return rows


def print_summary(rows: list[dict], md: bool = False) -> None:
    multi = any(r.get("trials", 1) > 1 for r in rows)
    head = ["split", "variant", "model", "runs", "all checks"] + (["per trial", "every trial"] if multi else []) + \
        ["format", "honesty", "safety", "useful", "unsafe", "p50 s", "cost $"]
    lines = []
    for r in rows:
        tr = r.get("trial_rates") or [r["all_pass"]]
        spread = ["{:.0f}–{:.0f}%".format(min(tr), max(tr)), "{:.0f}%".format(r.get("every_trial", r["all_pass"]))] if multi else []
        lines.append([r["split"], r["variant"], r["model_id"] or r["model"], str(r["runs"]), "{:.0f}%".format(r["all_pass"]),
                      *spread, *("{:.0f}%".format(r[g]) for g in ("format", "honesty", "safety", "usefulness")),
                      r["unsafe"], "{:.1f}".format(r["latency_p50"]), "{:.3f}".format(r["cost_total"])])
    if md:
        print("| " + " | ".join(head) + " |\n|" + "---|" * len(head))
        for ln in lines:
            print("| " + " | ".join(ln) + " |")
    else:
        w = [max(len(x) for x in col) for col in zip(head, *lines)]
        for ln in [head] + lines:
            print("  ".join(x.ljust(n) for x, n in zip(ln, w)))
    for r in rows:
        if r["fails"]:
            print("\n{} {} / {}: failed checks ".format(r["split"], r["variant"], r["model_id"] or r["model"]) +
                  ", ".join("{} ×{}".format(k, v) for k, v in r["fails"].items()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", default="insights", choices=("insights", "workout"),
                    help="insights: fitdash insights (today + week); workout: fitdash insights --workout")
    ap.add_argument("--variants", default="shipped", help="comma-separated: shipped, rules, or evals/prompts/<name>.txt")
    ap.add_argument("--models", default="default", help="comma-separated: default (the CLI's), sonnet, haiku, opus…")
    ap.add_argument("--scenarios", help="comma-separated scenario ids (default: all in --split)")
    ap.add_argument("--split", default="dev", choices=("dev", "holdout", "all"),
                    help="dev scenarios design the prompt; holdout ones only judge the final comparison")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--out", type=Path, help="where to save every run (default: evals/results/<timestamp>.json)")
    ap.add_argument("--report", type=Path, help="print the summary of a saved run instead of running")
    ap.add_argument("--md", action="store_true", help="summary as a Markdown table")
    ap.add_argument("--resume", type=Path, help="re-run only the failed calls (e.g. a usage limit) of a saved run, "
                                                "keep the rest, and save back to the same file")
    ap.add_argument("--regrade", action="store_true", help="with --report: re-run the checks on the saved replies "
                                                          "(after a grader fix), against today's scenario inputs")
    a = ap.parse_args()

    if a.report:
        saved = json.loads(a.report.read_text())
        if a.regrade:
            by = {s.id: s for s in SC.SCENARIOS}
            wby = {s.id: s for s in WS.WORKOUT_SCENARIOS}
            for r in saved["runs"]:
                if r.get("error"):
                    continue
                if r.get("task") == "workout" and r["scenario"] in wby:
                    ws = wby[r["scenario"]]
                    r["checks"] = {k: list(v) for k, v in W.run(r["raw"], CO.parse_workout(r["raw"]), WS.build(ws)[1],
                                                                ws).items()}
                elif r.get("task", "insights") == "insights" and r["scenario"] in by:
                    sc = by[r["scenario"]]
                    r["checks"] = {k: list(v) for k, v in K.run(r["raw"], CO.parse_insights(r["raw"]), SC.build(sc)[1],
                                                                sc.checks).items()}
        print_summary(summarize(saved["runs"]), a.md)
        return
    kept: list[dict] = []
    if a.resume:
        saved = json.loads(a.resume.read_text())
        kept = [r for r in saved["runs"] if not r.get("error")]
        for k in ("variants", "models", "scenarios", "split", "trials"):      # the grid the saved run asked for
            setattr(a, k, saved["args"][k])
        a.task = saved["args"].get("task", "insights")
        a.out = a.resume
    scs = WS.by_id(a.scenarios, a.split) if a.task == "workout" else SC.by_id(a.scenarios, a.split)
    run_one = one_workout if a.task == "workout" else one
    SC.base_model()                                   # build the fixture once, before the threads start
    jobs = [(sc, v, ("rules" if v == "rules" else mo), t) for v in a.variants.split(",")
            for mo in (["rules"] if v == "rules" else a.models.split(",")) for sc in scs for t in range(a.trials)]
    jobs = list(dict.fromkeys((sc.id, v, mo, t) for sc, v, mo, t in jobs))
    if a.resume:                                      # only what the saved run doesn't have yet
        have = {(r["scenario"], r["variant"], r["model"], r["trial"]) for r in kept}
        jobs = [j for j in jobs if j not in have]
    by = {sc.id: sc for sc in scs}
    print("{} runs ({} scenarios)…".format(len(jobs), len(scs)), file=sys.stderr)
    done = []
    limited = False
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        futs = [pool.submit(run_one, by[sid], v, mo, t, a.timeout) for sid, v, mo, t in jobs]
        for i, f in enumerate(futs, 1):
            r = f.result()
            done.append(r)
            if not limited and "limit" in str(r.get("error") or "").lower():
                limited = True                      # stop queuing: every later call would fail the same way
                print("  usage limit reached ({}); stopping. Re-run with --resume to finish.".format(r["error"][:60]),
                      file=sys.stderr)
                for g in futs[i:]:
                    g.cancel()
                done += [g.result() for g in futs[i:] if not g.cancelled()]     # calls already in flight
                break
            if i % 10 == 0 or i == len(futs):
                print("  {}/{}".format(i, len(futs)), file=sys.stderr)
    done = [r for r in done if r is not None]
    finished = {(r["scenario"], r["variant"], r["model"], r["trial"]) for r in done}
    done += [{"task": a.task, "scenario": sid, "split": by[sid].split, "variant": v, "model": mo, "trial": t, "error": "not run (usage limit)",
              "raw": "", "parsed": None, "checks": {}} for sid, v, mo, t in jobs if (sid, v, mo, t) not in finished]
    done = kept + done
    out = a.out or RESULTS / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"created": datetime.now().isoformat(timespec="seconds"), "args": vars(a) | {
        "out": str(out), "report": None}, "runs": done}, indent=1, default=str) + "\n")
    print_summary(summarize(done), a.md)
    print("\nsaved {}".format(out.relative_to(HERE.parent) if out.is_relative_to(HERE.parent) else out))


if __name__ == "__main__":
    main()
