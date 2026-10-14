"""A tiny MCP server used only by tests to provoke client-side failure handling.

Run as ``python tests/support/mcp_test_server.py``. It has no database and no
document access; each tool exists to trigger one behaviour of ``McpToolClient``.
"""

import os
import time
from typing import Annotated

from mcp.server import MCPServer
from pydantic import BaseModel, Field

mcp = MCPServer("clearcase-test-server")


class Echo(BaseModel):
    value: str


class Big(BaseModel):
    blob: str


@mcp.tool()
def echo(value: Annotated[str, Field(max_length=20)]) -> Echo:
    """Return the value."""
    return Echo(value=value)


@mcp.tool()
def slow(seconds: Annotated[float, Field(ge=0, le=30)]) -> Echo:
    """Sleep, then answer."""
    time.sleep(seconds)
    return Echo(value="done")


@mcp.tool()
def big(chars: Annotated[int, Field(ge=1, le=1_000_000)]) -> Big:
    """Return a very large result."""
    return Big(blob="x" * chars)


@mcp.tool()
def crash() -> Echo:
    """Kill the server process without answering."""
    os._exit(3)


if __name__ == "__main__":
    mcp.run()
