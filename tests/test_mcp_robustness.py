"""Production robustness checks over the real stdio server: malformed and unknown JSON-RPC, bad
arguments, cancellation, a large dataset (response size and latency), and concurrent calls.
Synthetic data only; no provider account or model is involved.

Response budget: Claude Code warns when one MCP tool result exceeds 10,000 tokens and truncates at
25,000 by default (MAX_MCP_OUTPUT_TOKENS). A result should stay well under the warning, estimated
here at 4 characters per token.
"""
from __future__ import annotations

import asyncio
import json
import select
import subprocess
import sys
import time
from datetime import date, timedelta

import pytest

from fitbit_mcp.config import load_settings
from fitbit_mcp.parsers.base import Rec
from fitbit_mcp.store import Store

from mcp import Client, StdioServerParameters
from test_mcp_protocol import server_env

TOKEN_WARNING = 10_000
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "robustness-test", "version": "1"}}}


class Raw:
    """A raw newline-delimited JSON-RPC session with the server subprocess."""

    def __init__(self, tmp_path):
        self.p = subprocess.Popen([sys.executable, "-m", "fitbit_mcp.server"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=server_env(tmp_path))
        self.send(INIT)
        assert "result" in self.read(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, msg):
        self.p.stdin.write(((msg if isinstance(msg, str) else json.dumps(msg)) + "\n").encode())
        self.p.stdin.flush()

    def read(self, want_id, timeout=20):
        """The response with this id; also returns id-less error responses (e.g. parse errors) when want_id is None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not select.select([self.p.stdout], [], [], 0.2)[0]:
                continue                                 # never block past the deadline on a dropped request
            line = self.p.stdout.readline()
            if not line:
                break
            msg = json.loads(line)                       # stdout stays pure JSON-RPC even after bad input
            if msg.get("id") == want_id:
                return msg
        raise AssertionError("no response with id {!r}".format(want_id))

    def call(self, i, method, params=None):
        self.send({"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})})
        return self.read(i)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)
        return self.p.returncode


@pytest.fixture
def raw(tmp_path):
    session = Raw(tmp_path)
    yield session
    assert session.close() == 0                         # clean exit when the client closes stdin


def alive(raw, i):
    """The session still answers a normal request."""
    assert len(raw.call(i, "tools/list")["result"]["tools"]) == 13


def test_malformed_json_does_not_break_the_session(raw):
    raw.send("{this is not json")
    raw.send('{"jsonrpc": "2.0", "id": 2, "method": ')            # truncated
    alive(raw, 3)


# Known deviations, from the MCP Python SDK's stdio reader rather than this server's code: invalid input is
# logged and dropped instead of answered. A client waiting on such a request would hang until its timeout.
# strict=True makes these fail loudly once an SDK upgrade fixes them, so the coverage map gets updated.
@pytest.mark.xfail(strict=True, reason="SDK drops unparseable lines; JSON-RPC asks for a -32700 response")
def test_malformed_json_gets_a_parse_error_response(raw):
    raw.send("{this is not json")
    assert raw.read(None, timeout=3)["error"]["code"] == -32700


@pytest.mark.xfail(strict=True, reason="SDK drops a request whose params isn't an object; JSON-RPC asks for an error")
def test_non_object_params_get_an_error_response(raw):
    assert "error" in _call_quick(raw, 2)


def test_unknown_method_is_method_not_found(raw):
    err = raw.call(2, "resources/subscribe_everything", {})["error"]
    assert err["code"] == -32601
    alive(raw, 3)


def _call_quick(raw, i):
    raw.send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": "not-an-object"})
    return raw.read(i, timeout=3)


def test_invalid_request_shapes_are_errors_not_crashes(raw):
    assert raw.call(2, "tools/call", {"arguments": {}})["error"]["code"] == -32602     # no tool name
    raw.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": "not-an-object"})   # dropped (see xfail)
    alive(raw, 4)


@pytest.mark.parametrize("name, args", [
    ("no_such_tool", {}),
    ("query_data", {"limit": "many"}),
    ("query_data", {"limit": 10_000}),                           # above MAX_QUERY_LIMIT
    ("query_data", {"metric": "x" * 1_000_000}),                 # a 1 MB argument
    ("summarize_data", {"metric": "steps", "period": "fortnight"}),
    ("job_status", {"job_id": None, "limit": -1}),
])
def test_bad_tool_arguments_return_tool_errors(raw, name, args):
    res = raw.call(2, "tools/call", {"name": name, "arguments": args})
    if "error" in res:                                         # SDK-level validation error is also acceptable
        assert res["error"]["code"] in (-32602, -32600, -32601)
    else:
        assert res["result"]["isError"] is True and res["result"]["content"][0]["text"]
    alive(raw, 3)


def test_cancellation_notifications_are_tolerated(raw):
    raw.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 999, "reason": "test"}})
    raw.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_data_types", "arguments": {}}})
    raw.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2, "reason": "user"}})
    alive(raw, 3)                                              # whether or not 2 was answered, the session lives


# ---------------------------------------------------------------- scale

YEARS = 5


def seed_large(tmp_path):
    """Five years of daily steps, distance, calories and resting HR, plus 30 days of minute heart rate."""
    settings = load_settings(tmp_path / "mcp-home")
    store = Store(settings.db_path, settings.home)
    start = date(2026, 10, 7) - timedelta(days=365 * YEARS)
    days = [(start + timedelta(days=i)).isoformat() for i in range(365 * YEARS)]
    daily = [Rec(category=cat, metric=m, granularity="daily", record_key=d, local_date=d, value=v(i), unit=u,
                 time_basis="date_only")
             for cat, m, u, v in (("activity", "steps", "count", lambda i: 6000 + i % 5000),
                                  ("activity", "distance", "m", lambda i: 4000.0 + i % 3000),
                                  ("activity", "calories_out", "kcal", lambda i: 2200 + i % 600),
                                  ("heart", "resting_heart_rate", "bpm", lambda i: 55 + i % 8))
             for i, d in enumerate(days)]
    store.upsert("google", "robustness-synthetic", daily, None)
    samples = []
    for k in range(30 * 24 * 60):
        t = f"2026-09-{1 + k // 1440:02d}T{(k // 60) % 24:02d}:{k % 60:02d}:00"
        samples.append(Rec(category="heart", metric="heart_rate", granularity="sample", record_key=t,
                           local_date=t[:10], start_local=t, value=60 + k % 40, unit="bpm"))
    store.upsert("google", "robustness-synthetic", samples, None)
    return len(daily) + len(samples)


def test_large_history_stays_fast_and_within_the_token_budget(tmp_path):
    n = seed_large(tmp_path)
    assert n > 50_000

    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=["-m", "fitbit_mcp.server"], env=server_env(tmp_path))
        async with Client(params) as client:
            calls = {
                "list_data_types": {},
                "query_data default page": {"metric": "heart_rate"},
                "query_data max page": {"metric": "heart_rate", "limit": 500},
                "summarize_data 5 years": {"metric": "steps", "start_date": "2021-10-08", "end_date": "2026-10-07",
                                           "period": "month"},
                "summarize_data default": {"metric": "resting_heart_rate"},
                "job_status": {},
                "connection_status": {},
            }
            report = {}
            for label, args in calls.items():
                tool = label.split()[0]
                t0 = time.perf_counter()
                res = await client.call_tool(tool, args)
                elapsed = time.perf_counter() - t0
                assert not res.is_error, (label, res.content)
                tokens = len(json.dumps(res.structured_content)) // 4
                report[label] = (round(elapsed, 2), tokens)
                assert tokens < TOKEN_WARNING, (label, tokens)
                assert elapsed < 3.0, (label, elapsed)
            print("\nlatency s, est. tokens:", json.dumps(report))
            # a page cut for size says so and continues losslessly through next_cursor
            seen, cursor, pages = set(), None, 0
            while True:
                res = (await client.call_tool("query_data", {"metric": "heart_rate", "limit": 500, "start_date": "2026-09-01",
                                                             "end_date": "2026-09-01", **({"cursor": cursor} if cursor else {})})
                       ).structured_content
                pages += 1
                assert res["count"] == 500 or "page_trimmed" in res or "next_cursor" not in res
                seen.update(r["id"] for r in res["records"])
                cursor = res.get("next_cursor")
                if not cursor:
                    break
            assert len(seen) == 1440 and pages > 1440 // 500                # one full day of minute samples
            # concurrency: 20 overlapping reads in one session all succeed and agree
            results = await asyncio.gather(*[client.call_tool("summarize_data", {"metric": "steps", "period": "week"})
                                             for _ in range(20)])
            assert all(not r.is_error for r in results)
            assert len({json.dumps(r.structured_content["overall"], sort_keys=True) for r in results}) == 1
    asyncio.run(scenario())


def test_two_server_processes_share_one_home(tmp_path):
    """Two hosts (say Claude Desktop and Claude Code) running the server against the same database."""
    seed_large(tmp_path)

    async def one():
        params = StdioServerParameters(command=sys.executable, args=["-m", "fitbit_mcp.server"], env=server_env(tmp_path))
        async with Client(params) as client:
            out = []
            for _ in range(5):
                res = await client.call_tool("query_data", {"metric": "steps", "limit": 50})
                assert not res.is_error
                out.append(res.structured_content["count"])
            return out

    async def both():
        return await asyncio.gather(one(), one())
    a, b = asyncio.run(both())
    assert a == b == [50] * 5
