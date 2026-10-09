"""End-to-end MCP checks over a real stdio subprocess (no Fitbit account involved).

These verify MCP initialization, tool discovery and representative tool calls through the
official Python MCP client, plus stdout purity. Live provider access is NOT exercised here.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from test_takeout import synthetic_takeout

from mcp import Client, StdioServerParameters

EXPECTED_TOOLS = {"connection_status", "connect_account", "start_import", "sync_data", "job_status", "cancel_job",
                  "resume_job", "list_data_types", "query_data", "summarize_data", "export_data", "import_takeout",
                  "disconnect_account"}


def server_env(tmp_path):
    env = dict(os.environ, FITBIT_MCP_HOME=str(tmp_path / "mcp-home"), FITBIT_MCP_TODAY="2026-10-07",
               FITBIT_MCP_GHEALTH_DIR=str(tmp_path / "no-ghealth"), FITBIT_MCP_TZ="UTC")
    env.pop("FITBIT_CLIENT_ID", None)
    return env


def test_stdio_initialize_discover_and_call_tools(tmp_path):
    archive = synthetic_takeout(tmp_path)

    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=["-m", "fitbit_mcp.server"],
                                       env=server_env(tmp_path))
        async with Client(params) as client:
            assert client.server_info.name == "fitbit-local"
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == EXPECTED_TOOLS
            assert tools["query_data"].annotations.read_only_hint is True
            assert tools["disconnect_account"].annotations.destructive_hint is True
            assert "start_date" in tools["start_import"].input_schema["properties"]

            status = (await client.call_tool("connection_status", {})).structured_content
            assert status["summary"] == "No verified API connection."
            assert all(p["connected"] is False for p in status["providers"])

            # API tools refuse cleanly without configuration (no crash, actionable message)
            result = await client.call_tool("start_import", {"provider": "google"})
            text = result.content[0].text
            assert result.is_error and ("Not connected" in text or "No usable" in text)

            job = (await client.call_tool("import_takeout", {"path": str(archive)})).structured_content
            for _ in range(100):
                view = (await client.call_tool("job_status", {"job_id": job["job_id"]})).structured_content
                if view["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.1)
            assert view["status"] == "succeeded", view

            types = (await client.call_tool("list_data_types", {})).structured_content
            assert any(m["metric"] == "steps" and m["provider"] == "fitbit_takeout" for m in types["metrics"])

            rows = (await client.call_tool("query_data", {"metric": "steps", "provider": "fitbit_takeout",
                                                          "limit": 1})).structured_content
            assert rows["count"] == 1 and "next_cursor" in rows
            page2 = (await client.call_tool("query_data", {"metric": "steps", "provider": "fitbit_takeout",
                                                           "limit": 1, "cursor": rows["next_cursor"]})
                     ).structured_content
            assert page2["records"][0]["id"] != rows["records"][0]["id"]

            summary = (await client.call_tool("summarize_data", {"metric": "steps", "start_date": "2026-10-01",
                                                                 "end_date": "2026-10-03", "period": "day"})
                       ).structured_content
            assert summary["definition"] and summary["overall"]["days_with_data"] >= 1

            exported = (await client.call_tool("export_data", {"format": "json", "metric": "steps"})
                        ).structured_content
            assert Path(exported["path"]).exists() and exported["records"] == 3

            bad = await client.call_tool("query_data", {"start_date": "2026-02-30"})
            assert bad.is_error
            bad = await client.call_tool("summarize_data", {"metric": "DROP TABLE"})
            assert bad.is_error

            preview = (await client.call_tool("disconnect_account", {"provider": "fitbit",
                                                                     "delete_local_data": True,
                                                                     "include_takeout_data": True})
                       ).structured_content
            assert preview["confirmation_required"] is True and preview["confirmation_token"]
            forged = await client.call_tool("disconnect_account", {"provider": "fitbit", "delete_local_data": True,
                                                                   "include_takeout_data": True,
                                                                   "confirmation_token": "guessed"})
            assert forged.is_error
            mismatched = await client.call_tool("disconnect_account", {"provider": "google",
                                                                       "delete_local_data": True,
                                                                       "confirmation_token":
                                                                           preview["confirmation_token"]})
            assert mismatched.is_error                       # token is bound to the exact arguments
            still = (await client.call_tool("query_data", {"provider": "fitbit_takeout"})).structured_content
            assert still["count"] > 0                        # nothing deleted without confirmation
            fresh = (await client.call_tool("disconnect_account", {"provider": "fitbit",
                                                                   "delete_local_data": True,
                                                                   "include_takeout_data": True})
                     ).structured_content
            done = (await client.call_tool("disconnect_account", {
                "provider": "fitbit", "delete_local_data": True, "include_takeout_data": True,
                "confirmation_token": fresh["confirmation_token"]})).structured_content
            assert done["deleted"]["records_deleted"] > 0
            assert (await client.call_tool("query_data", {"provider": "fitbit_takeout"})
                    ).structured_content["count"] == 0
            google_left = (await client.call_tool("query_data", {"provider": "google_takeout"})).structured_content
            assert google_left["count"] == 1                 # other providers' data untouched
    asyncio.run(scenario())


WRAPPER = r'''
import os, sys
sys.argv = ["fitbit-mcp"]
from fitbit_mcp import queries, server
original = queries.data_types
def noisy(store):
    print("STRAY PRINT FROM TOOL")            # must not reach the protocol stream
    os.system("echo STRAY CHILD PROCESS OUTPUT")
    return original(store)
queries.data_types = noisy
server.main()
'''


def test_stdout_contains_only_jsonrpc_even_with_stray_output(tmp_path):
    script = tmp_path / "wrapper.py"
    script.write_text(WRAPPER)
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=server_env(tmp_path))
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "raw-test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_data_types", "arguments": {}}},
    ]
    try:
        for message in messages:
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()
            time.sleep(0.3)
        deadline = time.time() + 20
        lines = []
        while time.time() < deadline and len([m for m in lines if "id" in m]) < 3:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(json.loads(line))          # every stdout line must be valid JSON
    finally:
        process.stdin.close()
        process.wait(timeout=10)
    stderr = process.stderr.read().decode()
    for line in process.stdout.read().decode().splitlines():   # includes anything flushed at shutdown
        if line.strip():
            lines.append(json.loads(line))
    assert all(m.get("jsonrpc") == "2.0" for m in lines)
    by_id = {m["id"]: m for m in lines if "id" in m}
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(by_id[2]["result"]["tools"]) == len(EXPECTED_TOOLS)
    assert "metrics" in by_id[3]["result"]["structuredContent"]
    assert "STRAY PRINT FROM TOOL" in stderr and "STRAY CHILD PROCESS OUTPUT" in stderr
