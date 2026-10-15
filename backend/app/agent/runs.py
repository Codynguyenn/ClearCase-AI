"""Starting, running, cancelling and finishing assistant runs (docs/phase5_design.md sections 3, 5 and 6).

A run is started by ``POST .../messages``. The request returns at once (202) with the stored user message and a run
record; the work happens in a background task inside this process, the same pattern document extraction uses. The
browser polls ``GET /runs/{id}`` for progress and the final answer.

Responsibilities, and what is kept separate on purpose:

* ``RunManager`` owns the lifecycle (records, one active run per conversation, a global concurrency cap, cancel,
  shutdown). It knows nothing about prompts or providers: it asks a factory for an ``Assistant``.
* ``ToolSessionPool`` keeps one MCP server process per conversation, so the second policy question does not pay the
  ~20 s model load again. A session is opened and closed by one dedicated task (the MCP/anyio transport requires the
  task that opened it to close it), reused while the conversation is active, closed when idle, and restarted once if
  the server process dies mid-call.
* The assistant itself (``AgentLoop`` for live mode, the scripted demo assistant for demo mode) is built per run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent.anthropic_provider import create_provider
from app.agent.citations import verify_citations
from app.agent.loop import (
    AgentLimits,
    AgentLoop,
    LoopResult,
    ToolEventCallback,
    ToolEventDraft,
    ToolRunner,
    failure_result,
)
from app.agent.mcp_client import McpToolClient, ToolCallResult, ToolSpec
from app.agent.provider import ProviderError, user_message_for
from app.config import Settings
from app.db import Mongo
from app.models.conversations import (
    MAX_ASSISTANT_MESSAGE_CHARS,
    AgentRun,
    AssistantMode,
    Message,
    MessageRole,
    MessageStatus,
    RunStatus,
    SendResult,
)
from app.repositories.conversations import (
    ConversationRepository,
    MessageRepository,
    RunRepository,
    ToolEventRepository,
)
from app.repositories.documents import DocumentRepository
from app.services.errors import AppError, ConflictError, ValidationFailed

logger = logging.getLogger("clearcase.agent.runs")

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SHUTDOWN_GRACE_SECONDS = 10.0
SHUTDOWN_TEXT = "The server was shut down while this request was running, so it was stopped. Please send it again."


class Assistant(Protocol):
    """What the run manager needs: both ``AgentLoop`` (live) and the demo assistant provide it."""

    async def run(
        self,
        *,
        document_id: str,
        history: Sequence[tuple[str, str]],
        user_text: str,
        cancel: asyncio.Event | None = None,
        on_tool_event: ToolEventCallback | None = None,
    ) -> LoopResult: ...


AssistantFactory = Callable[[Settings, ToolRunner], Assistant]
ToolContextFactory = Callable[[Settings], AbstractAsyncContextManager[ToolRunner]]


def default_assistant_factory(settings: Settings, tools: ToolRunner) -> Assistant:
    if settings.assistant_mode == "live":
        return AgentLoop(create_provider(settings), tools, AgentLimits.from_settings(settings))
    from app.agent.demo import DemoAssistant  # imported here: demo mode needs no provider or key

    return DemoAssistant(tools)


def default_tool_factory(settings: Settings) -> AbstractAsyncContextManager[ToolRunner]:
    return McpToolClient(settings)


# --------------------------------------------------------------------------------------------- tool sessions
class _ToolSession:
    """One tool server, opened and closed by a single dedicated task."""

    def __init__(self, settings: Settings, factory: ToolContextFactory) -> None:
        self._settings = settings
        self._factory = factory
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[ToolRunner] | None = None
        self._stop = asyncio.Event()

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def open(self) -> ToolRunner:
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._host(), name="tool-session")
        return await self._ready

    async def _host(self) -> None:
        assert self._ready is not None
        try:
            async with self._factory(self._settings) as runner:
                self._ready.set_result(runner)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - this task's only job is to open and close; report and end
            if not self._ready.done():
                self._ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                logger.warning("Tool session ended with %s", type(exc).__name__)

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=SHUTDOWN_GRACE_SECONDS)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task


class _PooledTools:
    """The ``ToolRunner`` a run sees. If the tool server dies mid-call it is restarted once and the call retried.

    The retry is safe because every tool is read-only, idempotent (proposals have deterministic ids) or returns the same
    export on repeat.
    """

    def __init__(self, pool: ToolSessionPool, conversation_id: str, runner: ToolRunner) -> None:
        self._pool = pool
        self._cid = conversation_id
        self._runner = runner

    @property
    def tools(self) -> list[ToolSpec]:
        return self._runner.tools

    async def call(self, name: str, arguments: Any) -> ToolCallResult:
        result = await self._runner.call(name, arguments)
        if result.error_code == "transport_error":
            logger.warning("Tool server failed during %s; restarting it once", name)
            self._runner = await self._pool.restart(self._cid)
            result = await self._runner.call(name, arguments)
        return result


class ToolSessionPool:
    def __init__(self, settings: Settings, factory: ToolContextFactory, idle_seconds: float) -> None:
        self._settings = settings
        self._factory = factory
        self._idle_seconds = idle_seconds
        self._sessions: dict[str, tuple[_ToolSession, ToolRunner]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_used: dict[str, float] = {}
        self._in_use: set[str] = set()

    def _lock(self, cid: str) -> asyncio.Lock:
        return self._locks.setdefault(cid, asyncio.Lock())

    async def acquire(self, cid: str) -> _PooledTools:
        async with self._lock(cid):
            entry = self._sessions.get(cid)
            if entry is None or not entry[0].alive:
                entry = await self._open(cid)
            self._in_use.add(cid)
            return _PooledTools(self, cid, entry[1])

    def release(self, cid: str) -> None:
        self._in_use.discard(cid)
        self._last_used[cid] = time.monotonic()

    async def restart(self, cid: str) -> ToolRunner:
        async with self._lock(cid):
            old = self._sessions.pop(cid, None)
            if old is not None:
                await old[0].close()
            return (await self._open(cid))[1]

    async def _open(self, cid: str) -> tuple[_ToolSession, ToolRunner]:
        session = _ToolSession(self._settings, self._factory)
        runner = await session.open()
        self._sessions[cid] = (session, runner)
        self._last_used[cid] = time.monotonic()
        return self._sessions[cid]

    async def sweep(self) -> int:
        """Close sessions nobody has used for a while. Returns how many were closed."""
        now = time.monotonic()
        stale = [c for c in self._sessions if c not in self._in_use and now - self._last_used.get(c, now) >= self._idle_seconds]
        for cid in stale:
            async with self._lock(cid):
                entry = self._sessions.pop(cid, None)
                if entry is not None:
                    await entry[0].close()
        return len(stale)

    async def close_all(self) -> None:
        for cid in list(self._sessions):
            entry = self._sessions.pop(cid, None)
            if entry is not None:
                await entry[0].close()

    @property
    def open_sessions(self) -> int:
        return len(self._sessions)


# --------------------------------------------------------------------------------------------- run manager
@dataclass
class _Prepared:
    owner_id: str
    conversation_id: str
    document_id: str
    content: str
    history: list[tuple[str, str]]
    message: Message
    run: AgentRun
    duplicate: bool


@dataclass
class _ActiveRun:
    cancel: asyncio.Event
    task: asyncio.Task[None] | None = None
    shutting_down: bool = False  # set when the server itself is stopping, so the notice says so
    recorded: set[int] = field(default_factory=set)  # tool events already written while the request was running


class RunManager:
    def __init__(
        self,
        mongo: Mongo,
        settings: Settings,
        *,
        assistant_factory: AssistantFactory | None = None,
        tool_factory: ToolContextFactory | None = None,
        max_concurrent: int | None = None,
        idle_seconds: float | None = None,
        sweep_interval: float = 60.0,
    ) -> None:
        self._settings = settings
        self._custom_assistant = assistant_factory is not None
        self._assistant_factory = assistant_factory or default_assistant_factory
        idle = settings.agent_tool_idle_seconds if idle_seconds is None else idle_seconds
        self._pool = ToolSessionPool(settings, tool_factory or default_tool_factory, idle)
        self._semaphore = asyncio.Semaphore(max_concurrent or settings.agent_max_concurrent_runs)
        self._sweep_interval = sweep_interval
        self._active: dict[str, _ActiveRun] = {}
        self._sweeper: asyncio.Task[None] | None = None
        self._conversations = ConversationRepository(mongo.db)
        self._messages = MessageRepository(mongo.db)
        self._runs = RunRepository(mongo.db)
        self._events = ToolEventRepository(mongo.db)
        self._documents = DocumentRepository(mongo.db)

    # ------------------------------------------------------------------------------ lifecycle
    async def start_background(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="tool-session-sweeper")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            try:
                closed = await self._pool.sweep()
                if closed:
                    logger.info("Closed %d idle tool session(s)", closed)
            except Exception:  # noqa: BLE001 - housekeeping must never stop the app
                logger.exception("Tool session sweep failed")

    async def shutdown(self) -> None:
        for active in self._active.values():
            active.shutting_down = True
            active.cancel.set()
        tasks = [a.task for a in self._active.values() if a.task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        await self._pool.close_all()

    @property
    def pool(self) -> ToolSessionPool:
        return self._pool

    # ------------------------------------------------------------------------------ starting a run
    def _check_configured(self) -> None:
        """Live mode needs a provider. Say so now, before anything is stored, instead of failing a run later."""
        if self._custom_assistant or self._settings.assistant_mode != "live":
            return
        try:
            create_provider(self._settings)
        except ProviderError as exc:
            raise AppError(user_message_for(exc), code="assistant_not_configured", status_code=409) from None

    @staticmethod
    def _clean(content: str) -> str:
        text = content.strip()
        if not text:
            raise ValidationFailed("Type a message first.", code="empty_message")
        if _CONTROL_CHARS.search(text):
            raise ValidationFailed("That message contains control characters.", code="invalid_message")
        return text

    def _prepare(self, owner_id: str, conversation_id: str, content: str, client_message_id: str | None) -> _Prepared:
        """Validate, then store the user's message and a queued run. Runs in a worker thread (it uses the sync driver)."""
        conversation = self._conversations.get(owner_id, conversation_id)  # 404 unless owned
        document = self._documents.get(conversation.document_id, owner_id=owner_id)
        if document.status.value not in ("ready", "unsupported"):
            raise ConflictError(
                f"This document is '{document.status.value}', so the assistant cannot work on it yet.", code="document_not_ready"
            )

        def already_sent(message: Message) -> _Prepared:
            assert message.run_id is not None  # every user message is stored together with its run
            run = self._runs.get(owner_id, message.run_id)
            return _Prepared(owner_id, conversation_id, conversation.document_id, content, [], message, run, duplicate=True)

        if client_message_id is not None:
            earlier = self._messages.find_by_client_id(owner_id, conversation_id, client_message_id)
            if earlier is not None:  # a retried request: hand back what was stored the first time
                return already_sent(earlier)

        live = self._settings.assistant_mode == "live"
        history = self._messages.recent_for_model(owner_id, conversation_id, limit=self._settings.agent_history_messages)
        run = self._runs.create(  # raises ConflictError("run_active") if one is already running here
            owner_id=owner_id,
            conversation_id=conversation_id,
            document_id=conversation.document_id,
            mode=AssistantMode(self._settings.assistant_mode),
            provider=(self._settings.llm_provider.strip() or None) if live else None,
            model=(self._settings.llm_model.strip() or None) if live else None,
        )
        message, created = self._messages.append(
            owner_id=owner_id,
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=content,
            run_id=run.id,
            client_message_id=client_message_id,
        )
        if not created:  # two identical requests raced and the database let exactly one message through
            self._runs.finish(owner_id, run.id, status=RunStatus.CANCELLED, error_code="duplicate")
            return already_sent(message)
        return _Prepared(owner_id, conversation_id, conversation.document_id, content, history, message, run, duplicate=False)

    async def start(self, *, owner_id: str, conversation_id: str, content: str, client_message_id: str | None) -> SendResult:
        text = self._clean(content)
        self._check_configured()
        prepared = await asyncio.to_thread(self._prepare, owner_id, conversation_id, text, client_message_id)
        if prepared.duplicate:
            return SendResult(message=prepared.message, run=prepared.run, duplicate=True)

        active = _ActiveRun(cancel=asyncio.Event())
        active.task = asyncio.create_task(self._execute(prepared, active), name=f"run-{prepared.run.id}")
        self._active[prepared.run.id] = active
        active.task.add_done_callback(lambda _t, rid=prepared.run.id: self._active.pop(rid, None))
        return SendResult(message=prepared.message, run=prepared.run, duplicate=False)

    # ------------------------------------------------------------------------------ cancelling
    async def cancel(self, owner_id: str, run_id: str) -> AgentRun:
        run = await asyncio.to_thread(self._runs.get, owner_id, run_id)  # 404 unless owned
        active = self._active.get(run_id)
        if active is not None:
            active.cancel.set()  # the run task notices, stops its work and records the outcome
        return run

    # ------------------------------------------------------------------------------ executing
    async def _execute(self, p: _Prepared, active: _ActiveRun) -> None:
        owner, cid = p.owner_id, p.conversation_id
        holding = False
        try:
            async with self._semaphore:
                if active.cancel.is_set():
                    result = LoopResult(status=RunStatus.CANCELLED)
                    result.text = "Stopped at your request before it started."
                else:
                    await asyncio.to_thread(self._runs.mark_running, owner, p.run.id)
                    tools = await self._pool.acquire(cid)
                    holding = True
                    assistant = self._assistant_factory(self._settings, tools)
                    async def record(event: ToolEventDraft) -> None:
                        # Written as it happens, so polling shows progress during a long request.
                        await asyncio.to_thread(self._record_event, p, event)
                        active.recorded.add(event.seq)

                    result = await assistant.run(
                        document_id=p.document_id,
                        history=p.history,
                        user_text=p.content,
                        cancel=active.cancel,
                        on_tool_event=record,
                    )
                    if active.shutting_down and result.status is RunStatus.CANCELLED:
                        result.text = SHUTDOWN_TEXT
        except ProviderError as exc:  # setup problems found while building the assistant
            result = failure_result(exc)
        except asyncio.CancelledError:
            result = LoopResult(status=RunStatus.CANCELLED)
            result.text = SHUTDOWN_TEXT
            await self._finalize(p, result, active.recorded)
            raise
        except Exception as exc:  # noqa: BLE001 - the run must always end in a recorded state
            logger.exception("Run %s failed unexpectedly", p.run.id)
            code = "tools_unavailable" if holding is False else "internal_error"
            result = failure_result(ProviderError(code, type(exc).__name__))
        finally:
            if holding:
                self._pool.release(cid)
        await self._finalize(p, result, active.recorded)

    async def _finalize(self, p: _Prepared, result: LoopResult, recorded: set[int]) -> None:
        try:
            await asyncio.shield(asyncio.to_thread(self._finalize_sync, p, result, set(recorded)))
        except Exception:  # noqa: BLE001 - last resort: never leave the conversation "busy" forever
            logger.exception("Could not record the outcome of run %s", p.run.id)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self._runs.finish, p.owner_id, p.run.id, status=RunStatus.FAILED, error_code="internal_error"
                )

    def _record_event(self, p: _Prepared, event: ToolEventDraft) -> None:
        self._events.record(
            owner_id=p.owner_id,
            run_id=p.run.id,
            document_id=p.document_id,
            seq=event.seq,
            tool=event.tool,
            status=event.status,
            duration_ms=event.duration_ms,
            summary=event.summary,
            error_code=event.error_code,
        )

    def _finalize_sync(self, p: _Prepared, result: LoopResult, recorded: set[int]) -> None:
        for event in result.tool_events:
            if event.seq not in recorded:  # anything not already written while the request was running
                self._record_event(p, event)
        status = {
            RunStatus.COMPLETED: MessageStatus.COMPLETE,
            RunStatus.CANCELLED: MessageStatus.CANCELLED,
        }.get(result.status, MessageStatus.FAILED)
        content = (result.text or "No answer was produced.")[:MAX_ASSISTANT_MESSAGE_CHARS]
        citations, unverified = (
            verify_citations(content, result.evidence) if result.status is RunStatus.COMPLETED else ([], 0)
        )
        message, _ = self._messages.append(
            owner_id=p.owner_id,
            conversation_id=p.conversation_id,
            role=MessageRole.ASSISTANT,
            content=content,
            status=status,
            run_id=p.run.id,
            citations=citations,
        )
        self._runs.finish(
            p.owner_id,
            p.run.id,
            status=result.status,
            rounds=result.rounds,
            tool_calls=result.tool_calls,
            limits_hit=result.limits_hit,
            error_code=result.error_code,
            error=result.error,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            request_ids=result.request_ids,
            unverified_citations=unverified,
            assistant_message_id=message.id,
        )
