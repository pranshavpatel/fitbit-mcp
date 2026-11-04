"""Instrumented production server. Invoke with -I; never uses real accounts."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "src"), str(REPO / "tests")]


def write(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, default=str))
    temporary.replace(path)


def isolate(root, today):
    # Child process only. Keep no inherited account/provider/API credentials.
    keep = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update(FITBIT_MCP_HOME=str(root / "home"), FITBIT_MCP_GHEALTH_DIR=str(root / "ghealth"),
                      FITBIT_MCP_TODAY=today, FITBIT_MCP_TZ="America/New_York", FITBIT_MCP_AUTO_RESUME="0",
                      FITBIT_MCP_MIN_REQUEST_INTERVAL="0", FITBIT_MCP_JOB_HEARTBEAT_SECONDS="0.2")

    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            with (root / "violations.jsonl").open("a") as f:
                f.write(json.dumps({"event": event}) + "\n")
            raise PermissionError("Eval server network/process execution is blocked")
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(args[0])).resolve()
            # Prevent fallback credential discovery even if a production path regresses.
            forbidden = (Path.home() / ".fitbit-mcp", Path.home() / ".config/ghealth",
                         Path.home() / ".aws", Path.home() / ".codex")
            if any(p == x or x in p.parents for x in forbidden):
                raise PermissionError("Private account directories are unavailable to the eval server")
    sys.addaudithook(audit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--case", required=True)
    a = ap.parse_args()
    root = a.root.resolve()
    if not root.is_dir() or not (root / "eval-marker").exists():
        raise ValueError("a fresh marked synthetic trial directory is required")
    if (root / "home").exists():
        raise ValueError("refusing to reuse a trial home")
    from evals.mcp.cases import select
    case = select(a.case)[0]
    isolate(root, "2026-11-02" if case.fixture in {"dates", "dst_sleep"} else "2026-10-07")
    from evals.mcp.fixture import build, snapshot
    from fitbit_mcp.server import create_server, serve_stdio
    from fitbit_mcp.util import configure_logging
    from mcp.server.mcpserver.exceptions import ToolError
    from fitbit_mcp import client
    app, http, gate, meta = build(root, case)
    configure_logging(app.settings.logs_dir)
    server = create_server(app)
    delegate = server.call_tool
    discover = server.list_tools
    count = 0

    async def discovered():
        tools = await discover()
        write(root / "schemas.json", [t.model_dump(mode="json", by_alias=True) for t in tools])
        return tools

    server.list_tools = discovered

    async def traced(name, arguments, context=None):
        nonlocal count
        count += 1
        control = json.loads((root / "control.json").read_text())
        event = {"tool": name, "arguments": arguments, "user_turn": control["user_turn"], "index": count}
        if context is not None:
            event["protocol_version"] = context.request_context.protocol_version
        start = time.monotonic()
        try:
            if count > 30:
                event["blocked"] = "tool_budget"
                raise ToolError("Eval tool-call budget exceeded; stop and explain the limitation")
            destructive = name == "disconnect_account" and (arguments.get("confirmation_token") or
                          not (arguments.get("delete_local_data") or arguments.get("revoke_remote_authorization")))
            if destructive:
                allowed = (case.mutation in {"delete", "expired"} and control.get("approved") and
                           arguments.get("provider") == "google" and arguments.get("delete_local_data") is True and
                           not arguments.get("include_takeout_data") and not arguments.get("revoke_remote_authorization"))
                if not allowed:
                    event["blocked"] = "unauthorized_destructive_attempt"
                    raise ToolError("Eval guard blocked an unauthorized destructive request")
            if name == "import_takeout" and Path(arguments.get("path", "")).resolve() != Path(meta["archive"]):
                event["blocked"] = "unauthorized_file"
                raise ToolError("Only the supplied synthetic archive is available")
            if name == "resume_job":
                gate.update(limited=False, failed=False)
                client._NOT_BEFORE.clear()
            if case.fixture == "expire" and control["user_turn"] > 1 and arguments.get("confirmation_token"):
                # Expire existing previews once, simulating delay without waiting five minutes.
                if not (root / "expired-once").exists():
                    app.confirmations.clear()
                    (root / "expired-once").touch()
            result = await delegate(name, arguments, context)
            event["result"] = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            return result
        except Exception as error:
            event["error"] = str(error)
            raise
        finally:
            event["duration_s"] = round(time.monotonic() - start, 4)
            event["state"] = snapshot(app)
            with (root / "trace.jsonl").open("a") as f:
                f.write(json.dumps(event, default=str) + "\n")
            write(root / "state.json", event["state"])
            # Headers and token request bodies are intentionally not recorded.
            write(root / "provider-requests.json", [{"method": r[0], "url": r[1]} for r in http.calls])

    server.call_tool = traced
    write(root / "metadata.json", meta)
    write(root / "state.json", meta["initial"])
    serve_stdio(server)


if __name__ == "__main__":
    main()
