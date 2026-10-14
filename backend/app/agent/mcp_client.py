"""The backend's MCP client: how the assistant reaches the document tools.

The agent (Phase 5) never imports tool code. It starts the MCP server as a child
process, *discovers* the tools and their JSON Schemas over the protocol, and calls
them through an MCP client session. This module is the thin, safety-minded layer
between the agent loop and that session.

What it adds on top of the raw SDK client
-----------------------------------------
* **Discovery:** the tool list and schemas come from the server, not from a copy
  of the code.
* **Argument validation before sending:** arguments the model produced are checked
  against the discovered input schema. Unknown tools and unknown argument names
  (for example a made-up ``owner_id``) are rejected locally, with a message the
  model can act on.
* **Timeouts:** every call is bounded (the export tool gets longer).
* **Result-size cap:** an oversized result is refused instead of flooding the
  model's context.
* **No exceptions for expected failures:** every call returns a ``ToolCallResult``
  with ``ok`` and an ``error_code`` the agent can reason about. Only cancellation
  propagates.
* **Least privilege for the child process:** it receives only database settings and
  the demo owner id, never the LLM API key.

Retries: this client never retries by itself. The one tool that writes
(``propose_redactions``) is idempotent on the server, so a retry by the agent is
safe; the export tool returns the same export on retry.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from types import TracebackType
from typing import Any

import anyio
from jsonschema import Draft202012Validator
from mcp import Client, MCPError, StdioServerParameters
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT
from pydantic import BaseModel

from app.config import Settings

logger = logging.getLogger("clearcase.agent.mcp")

BACKEND_DIR = Path(__file__).resolve().parents[2]  # the folder that contains the `app` package

DEFAULT_TIMEOUT_SECONDS = 30.0
TOOL_TIMEOUTS: dict[str, float] = {"export_approved_redactions": 180.0}
MAX_RESULT_CHARS = 200_000
MAX_ERROR_CHARS = 500
MAX_SCHEMA_ERRORS = 5


class ToolSpec(BaseModel):
    """A tool as the server described it. This is what the model is shown."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class ToolCallResult(BaseModel):
    tool: str
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None  # unknown_tool | invalid_arguments | tool_error | timeout | result_too_large | malformed_result | transport_error
    error: str | None = None
    duration_ms: int = 0


def default_server_params(settings: Settings) -> StdioServerParameters:
    """How to start our own MCP server, passing it only what it needs."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=BACKEND_DIR,
        env={
            "CLEARCASE_MONGODB_URI": settings.mongodb_uri,
            "CLEARCASE_MONGODB_DB": settings.mongodb_db,
            "CLEARCASE_DEMO_OWNER_ID": settings.demo_owner_id,
        },
    )


class McpToolClient:
    """Use as ``async with McpToolClient(settings) as tools:``."""

    def __init__(
        self,
        settings: Settings,
        *,
        server_params: StdioServerParameters | None = None,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        timeouts: dict[str, float] | None = None,
        max_result_chars: int = MAX_RESULT_CHARS,
    ) -> None:
        self._params = server_params or default_server_params(settings)
        self._default_timeout = default_timeout
        self._timeouts = {**TOOL_TIMEOUTS, **(timeouts or {})}
        self._max_result_chars = max_result_chars
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._specs: dict[str, ToolSpec] = {}
        self._validators: dict[str, Draft202012Validator] = {}

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> McpToolClient:
        stack = AsyncExitStack()
        try:
            # Starts the server process and performs the MCP handshake.
            self._client = await stack.enter_async_context(Client(self._params))
            await self.refresh_tools()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    # ------------------------------------------------------------ discovery
    async def refresh_tools(self) -> list[ToolSpec]:
        client = self._require_client()
        specs: dict[str, ToolSpec] = {}
        cursor: str | None = None
        for _ in range(20):  # a paginator that never ends must not hang us
            page = await client.list_tools(cursor=cursor)
            for tool in page.tools:
                specs[tool.name] = ToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema),
                    output_schema=dict(tool.output_schema) if tool.output_schema else None,
                )
            cursor = page.next_cursor
            if cursor is None:
                break
        self._specs = specs
        self._validators = {name: Draft202012Validator(spec.input_schema) for name, spec in specs.items()}
        return self.tools

    @property
    def tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    # ----------------------------------------------------------------- calls
    def validate_arguments(self, name: str, arguments: Any) -> str | None:
        """Return a human-readable problem, or None if the arguments are acceptable."""
        spec = self._specs.get(name)
        if spec is None:
            return f"Unknown tool '{name}'. Available tools: {', '.join(sorted(self._specs))}."
        if not isinstance(arguments, dict):
            return "Tool arguments must be a JSON object."
        allowed = set(spec.input_schema.get("properties", {}))
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            return f"Unknown argument(s) for {name}: {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}."
        problems = []
        for err in sorted(self._validators[name].iter_errors(arguments), key=lambda e: list(e.absolute_path))[
            :MAX_SCHEMA_ERRORS
        ]:
            where = ".".join(str(p) for p in err.absolute_path) or "arguments"
            problems.append(f"{where}: {err.message}"[:200])
        return "; ".join(problems) if problems else None

    async def call(self, name: str, arguments: Any) -> ToolCallResult:
        client = self._require_client()  # misuse (not inside `async with`) must be loud, not "unknown tool"
        started = time.perf_counter()

        def done(**fields: Any) -> ToolCallResult:
            result = ToolCallResult(tool=name, duration_ms=int((time.perf_counter() - started) * 1000), **fields)
            # Safe by construction: never the arguments, never document text.
            logger.info("mcp_call tool=%s ok=%s code=%s duration_ms=%d", name, result.ok, result.error_code, result.duration_ms)
            return result

        if name not in self._specs:
            return done(ok=False, error_code="unknown_tool", error=self.validate_arguments(name, arguments))
        problem = self.validate_arguments(name, arguments)
        if problem:
            return done(ok=False, error_code="invalid_arguments", error=problem)

        timeout = self._timeouts.get(name, self._default_timeout)
        try:
            # Two layers: the SDK cancels the request at `timeout`; fail_after is the hard stop.
            with anyio.fail_after(timeout + 5):
                raw = await client.call_tool(name, arguments, read_timeout_seconds=timeout)
        except TimeoutError:
            return done(ok=False, error_code="timeout", error=_timeout_message(timeout))
        except anyio.get_cancelled_exc_class():
            raise
        except MCPError as exc:
            if exc.code == REQUEST_TIMEOUT:
                return done(ok=False, error_code="timeout", error=_timeout_message(timeout))
            logger.warning("mcp protocol error for %s: code=%s", name, exc.code)
            reason = "the tool server stopped" if exc.code == CONNECTION_CLOSED else "the protocol reported an error"
            return done(ok=False, error_code="transport_error", error=f"Could not complete the tool call: {reason}.")
        except Exception as exc:  # e.g. broken pipe while the server was dying
            logger.warning("mcp transport failure for %s: %s", name, type(exc).__name__)
            return done(ok=False, error_code="transport_error", error=f"Could not reach the tool server ({type(exc).__name__}).")

        if raw.is_error:
            text = " ".join(getattr(block, "text", "") for block in raw.content).strip()
            return done(ok=False, error_code="tool_error", error=(text or "The tool reported an error.")[:MAX_ERROR_CHARS])
        if not isinstance(raw.structured_content, dict):
            return done(ok=False, error_code="malformed_result", error="The tool returned no structured result.")
        size = len(json.dumps(raw.structured_content, separators=(",", ":")))
        if size > self._max_result_chars:
            return done(
                ok=False,
                error_code="result_too_large",
                error=f"The result was {size} characters, over the {self._max_result_chars} limit. Narrow the request.",
            )
        return done(ok=True, data=raw.structured_content)

    # --------------------------------------------------------------- helpers
    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("McpToolClient must be used inside 'async with'.")
        return self._client


def _timeout_message(seconds: float) -> str:
    return f"The tool did not answer within {seconds:.0f} seconds."


__all__ = ["McpToolClient", "ToolCallResult", "ToolSpec", "default_server_params"]
