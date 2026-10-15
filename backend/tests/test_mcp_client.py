"""Tests for ``McpToolClient``: the layer between the agent loop and the MCP session.

These use a tiny test-only server (``tests/support/mcp_test_server.py``) so each
failure mode (slow tool, huge result, crashed server) can be provoked on demand.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from app.agent.mcp_client import McpToolClient
from app.config import Settings

TEST_SERVER = Path(__file__).parent / "support" / "mcp_test_server.py"


@pytest.fixture
def params() -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=[str(TEST_SERVER)])


def run(coro):
    return asyncio.run(coro)


def test_discovers_tools_and_returns_structured_results(test_settings: Settings, params):
    async def scenario():
        async with McpToolClient(test_settings, server_params=params) as tools:
            names = {t.name for t in tools.tools}
            result = await tools.call("echo", {"value": "hi"})
            return names, result

    names, result = run(scenario())
    assert names == {"echo", "slow", "big", "crash"}
    assert result.ok and result.data == {"value": "hi"} and result.error_code is None


def test_arguments_are_validated_before_anything_is_sent(test_settings: Settings, params):
    async def scenario():
        async with McpToolClient(test_settings, server_params=params) as tools:
            return [
                await tools.call("echo", {"value": "x" * 21}),  # over maxLength
                await tools.call("echo", {}),  # missing
                await tools.call("echo", {"value": "a", "owner_id": "x"}),  # invented argument
                await tools.call("echo", "value"),  # not an object
                await tools.call("nope", {}),  # not a discovered tool
            ]

    codes = [r.error_code for r in run(scenario())]
    assert codes == ["invalid_arguments", "invalid_arguments", "invalid_arguments", "invalid_arguments", "unknown_tool"]


def test_a_slow_tool_times_out_and_the_client_stays_usable(test_settings: Settings, params):
    async def scenario():
        async with McpToolClient(test_settings, server_params=params, timeouts={"slow": 1.0}) as tools:
            slow = await tools.call("slow", {"seconds": 6})
            return slow

    result = run(scenario())
    assert not result.ok and result.error_code == "timeout"
    assert result.duration_ms < 5000


def test_an_oversized_result_is_refused_not_passed_on(test_settings: Settings, params):
    async def scenario():
        async with McpToolClient(test_settings, server_params=params, max_result_chars=1000) as tools:
            return await tools.call("big", {"chars": 5000}), await tools.call("big", {"chars": 100})

    too_big, fine = run(scenario())
    assert not too_big.ok and too_big.error_code == "result_too_large" and too_big.data is None
    assert fine.ok


def test_a_crashing_server_becomes_a_transport_error_not_an_exception(test_settings: Settings, params):
    async def scenario():
        async with McpToolClient(test_settings, server_params=params) as tools:
            return await tools.call("crash", {})

    result = run(scenario())
    assert not result.ok and result.error_code == "transport_error"


def test_using_the_client_outside_async_with_is_an_error(test_settings: Settings):
    tools = McpToolClient(test_settings)
    with pytest.raises(RuntimeError):
        run(tools.call("echo", {}))
