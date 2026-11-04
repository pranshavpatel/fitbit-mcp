"""Version-scoped shipped-stdio checks; not the official HTTP conformance suite."""
from __future__ import annotations

import asyncio
import sys

from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters

from test_mcp_protocol import EXPECTED_TOOLS, server_env


def test_discovered_schemas_results_and_invalid_arguments(tmp_path):
    async def scenario():
        async with Client(StdioServerParameters(command=sys.executable, args=["-m", "fitbit_mcp.server"],
                                                env=server_env(tmp_path))) as client:
            discovered = await client.list_tools()
            assert not discovered.next_cursor
            tools = {tool.name: tool for tool in discovered.tools}
            assert set(tools) == EXPECTED_TOOLS
            for tool in tools.values():
                Draft202012Validator.check_schema(tool.input_schema)
                assert tool.output_schema is not None
                Draft202012Validator.check_schema(tool.output_schema)
            for name, args in [("connection_status", {}), ("list_data_types", {}),
                               ("query_data", {"metric": "steps"}),
                               ("summarize_data", {"metric": "steps"}),
                               ("disconnect_account", {"provider": "google", "delete_local_data": True})]:
                result = await client.call_tool(name, args)
                assert not result.is_error
                Draft202012Validator(tools[name].output_schema).validate(result.structured_content)
            # Invalid input and domain errors are MCP tool errors, not fabricated success.
            for name, args in [("query_data", {"limit": "not-an-integer"}),
                               ("summarize_data", {"metric": "unsupported"}),
                               ("job_status", {"job_id": "missing-job"})]:
                result = await client.call_tool(name, args)
                assert result.is_error and result.content
    asyncio.run(scenario())
