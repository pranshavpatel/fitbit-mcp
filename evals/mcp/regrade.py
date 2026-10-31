"""Regrade saved evidence without model calls; preserve original results verbatim."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .cases import CASES
from .grade import grade
from .run import read, save, summary


def regrade(directory):
    original = read(directory / "results.json")
    cases = {c.id: c for c in CASES}
    updated, changes = [], []
    for saved in original["runs"]:
        with tempfile.TemporaryDirectory(prefix="mcp-regrade-") as temporary:
            root = Path(temporary)
            r = json.loads(json.dumps(saved).replace("$TRIAL", str(root)))
            artifact = directory / (r["case"] + "-" + str(r["trial"]))
            if (artifact / "exports").exists():
                shutil.copytree(artifact / "exports", root / "home/exports")
            score = grade(cases[r["case"]], r["trace"], r["answers"], r["initial"], r["final"], root)
            # Do not erase harness errors or critical violations captured by the original runner.
            score["critical_failures"] = sorted(set(score["critical_failures"] + r["critical_failures"]))
            score["checks"]["no_critical_failures"]["pass"] = not score["critical_failures"]
            score["passed"] = all(c["pass"] for c in score["checks"].values()) and not r["error"]
            next_result = {**saved, **score}
            updated.append(next_result)
            if score != {k: saved[k] for k in score}:
                changes.append({"case": r["case"], "trial": r["trial"], "before": saved["passed"],
                                "after": score["passed"], "critical_failures": score["critical_failures"]})
    report = {"grader_revision": "v2-post-baseline-audit", "original_untouched": True,
              "grader_sha256": hashlib.sha256(Path(__file__).with_name("grade.py").read_bytes()).hexdigest(),
              "original_results_sha256": hashlib.sha256((directory / "results.json").read_bytes()).hexdigest(),
              "changes": changes, "runs": updated,
              "summary": summary(updated, read(directory / "manifest.json")["args"]["trials"])}
    save(directory / "regraded-results.json", report)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directories", type=Path, nargs="+")
    args = p.parse_args()
    for directory in args.directories:
        r = regrade(directory)
        print(json.dumps({"directory": str(directory), "summary": r["summary"][-1]}))


if __name__ == "__main__":
    main()
