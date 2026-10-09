"""Run one incremental Google Health sync and wait for it, for `fitdash`'s auto-refresh.

Runs inside the fitbit-mcp project environment (it imports the `fitbit_mcp` package), so it reuses
the exact code path of the MCP `sync_data` tool: the same stored credentials, read-only (GET-only)
API access and job bookkeeping. Progress and the outcome go to stderr; nothing is printed to stdout.

    uv run --quiet --frozen --directory <repo> python sync_now.py [--timeout 180]

Exit codes: 0 synced (or another sync just finished), 1 sync failed, 2 couldn't start (e.g. not
connected or the Google login expired).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Don't let this short-lived process pick up interrupted imports left by a stopped server: a quick
# refresh must never turn into a long background import.
os.environ.setdefault("FITBIT_MCP_AUTO_RESUME", "0")

from fitbit_mcp import server  # noqa: E402

FINAL = ("succeeded", "partial", "failed", "cancelled", "interrupted")


def say(text: str, end: str = "\n") -> None:
    sys.stderr.write(text + end)
    sys.stderr.flush()


def wait(app, job_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = app.jobs.get(job_id)
        prog = (job or {}).get("progress") or {}
        if prog.get("requests_total") and sys.stderr.isatty():      # live progress only in a terminal
            say("\r\033[K  syncing Fitbit data… {}/{} requests".format(prog.get("requests_done", 0),
                                                                     prog["requests_total"]), end="")
        if job and job["status"] in FINAL:
            if sys.stderr.isatty():
                say("\r\033[K", end="")
            return job
        if time.monotonic() > deadline:
            if sys.stderr.isatty():
                say("\r\033[K", end="")
            return dict(job or {}, status="timeout")
        time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="google")
    ap.add_argument("--overlap-days", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    try:
        app = server.build_app()
    except Exception as exc:  # noqa: BLE001 - report, never crash fitdash
        say("  sync skipped: {}".format(exc))
        return 2
    try:
        running = [j for j in app.jobs.active(provider=args.provider) if j["kind"] in ("sync", "import")]
        if running:
            say("  a sync is already running; waiting for it…")
            job = wait(app, running[0]["id"], args.timeout)
        else:
            try:
                view = server._sync(app, args.provider, args.overlap_days, None, False)
            except Exception as exc:  # ToolError: not connected, token expired, nothing imported yet
                msg = str(exc) or exc.__class__.__name__
                say("  sync skipped: {}".format(msg))
                if any(w in msg.lower() for w in ("token", "expired", "login", "connect", "credential")):
                    say("  to reconnect: ghealth auth login")
                return 2
            job = wait(app, view["job_id"], args.timeout)
        status = job.get("status")
        recs = (job.get("progress") or {}).get("records") or {}
        if status == "succeeded":
            say("  ✓ synced ({} new, {} updated)".format(recs.get("inserted", 0), recs.get("updated", 0)))
            return 0
        if status == "partial":
            say("  ! sync partly succeeded; showing what arrived")
            return 0
        if status == "timeout":
            say("  ! sync didn't finish in {:.0f}s; showing local data (finished requests are kept, "
                "and the next sync picks up the rest)".format(args.timeout))
            return 1
        say("  ✕ sync {}: {}".format(status, job.get("error") or job.get("message") or ""))
        return 1
    finally:
        app.jobs.stop()


if __name__ == "__main__":
    sys.exit(main())
