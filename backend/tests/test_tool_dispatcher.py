"""Contract tests for :class:`bob.tools.dispatcher.ToolDispatcher`.

The dispatcher is the single point through which the orchestrator routes
tool calls, so the tests here pin the three failure modes that PRD 0006
explicitly names:

- **Unknown tool name.** Returns ``DispatchResult(outcome="error",
  error_code="unknown_tool", ...)`` and emits a ``jarvis.route`` event.
- **Schema-invalid arguments.** Returns ``DispatchResult(outcome="error",
  error_code="invalid_args", ...)`` and emits a route event.
- **Handler-reported domain error (e.g. unknown task_id).** Returns
  ``DispatchResult(outcome="error", ...)`` with the handler's
  ``error_code`` propagated; route event still emitted.

The happy path (handler returns ``status="ok"``) is asserted alongside
the route-event payload shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel, Field

from bob import debug_log
from bob.debug_log import DebugEvent
from bob.llm.types import ToolCall
from bob.tools.dispatcher import (
    JARVIS_ROUTE_EVENT_SOURCE,
    DispatchResult,
    ToolDispatcher,
    ToolHandlerContext,
)
from bob.tools.registry import ToolDefinition, ToolRegistry
from bob.tools.types import ToolHandlerOutcome


class _TwoFieldArgs(BaseModel):
    title: str = Field(..., min_length=1)
    goal: str = Field(..., min_length=1)


class _StubScheduler:
    async def enqueue(self, task_id: str) -> None:
        pass

    async def resume(self, task_id: str) -> None:
        pass

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        pass


class _StubTaskStore:
    def create_task(
        self,
        *,
        title: str,
        goal: str,
        parent_task_id: str | None = None,
        lineage: Any = None,
        scope: Any = None,
    ) -> str:
        return "task-stub"

    def get_task(self, task_id: str) -> Any:
        raise NotImplementedError

    def list_tasks(self, *, state: Any = None, limit: Any = None) -> Any:
        return []

    def append_message(
        self,
        task_id: str,
        *,
        role: Any,
        content: str,
        action: Any = None,
    ) -> int:
        return 1

    def get_task_messages(self, task_id: str) -> Any:
        return []

    def update_state(self, task_id: str, new_state: Any) -> None:
        return None

    def set_result(self, task_id: str, result: str) -> None:
        return None

    def set_delivered_at_turn(self, task_id: str, turn_index: int) -> None:
        return None

    def mark_superseded(self, task_id: str) -> None:
        return None

    def find_by_query(
        self,
        query: str,
        *,
        prefer_state: Any = None,
        limit: int = 1,
    ) -> Any:
        return []


async def _noop_emit(event: dict[str, Any]) -> None:
    return None


def _make_dispatcher(
    handler: Any,
    *,
    name: str = "stub_tool",
    version: str = "v1",
) -> tuple[ToolDispatcher, ToolDefinition]:
    definition = ToolDefinition(
        name=name,
        version=version,
        description="stub for tests",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "goal": {"type": "string"},
            },
            "required": ["title", "goal"],
        },
        args_model=_TwoFieldArgs,
        handler=handler,
    )
    registry = ToolRegistry([definition])
    context = ToolHandlerContext(
        task_store=_StubTaskStore(),
        task_scheduler=_StubScheduler(),
        ws_emit=_noop_emit,
    )
    return ToolDispatcher(registry, context), definition


def _snapshot_route_events() -> list[DebugEvent]:
    return [event for event in debug_log.snapshot() if event.source == JARVIS_ROUTE_EVENT_SOURCE]


@pytest.fixture(autouse=True)
def _reset_debug_buffer() -> Iterator[None]:
    debug_log.clear()
    yield
    debug_log.clear()


@pytest.mark.asyncio
async def test_dispatch_happy_path_returns_ok_and_emits_route_event() -> None:
    async def _handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(status="ok", task_id="task-42")

    dispatcher, _ = _make_dispatcher(_handler)

    result = await dispatcher.dispatch(
        ToolCall(id="call_1", name="stub_tool", arguments={"title": "T", "goal": "G"})
    )

    assert result == DispatchResult(
        outcome="ok",
        tool_name="stub_tool",
        tool_version="v1",
        task_id="task-42",
    )

    events = _snapshot_route_events()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["tool"] == "stub_tool"
    assert payload["version"] == "v1"
    assert payload["outcome"] == "ok"
    assert payload["task_id"] == "task-42"
    assert payload["error_code"] is None
    assert payload["argument_keys"] == ["goal", "title"]


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error_result() -> None:
    async def _handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(status="ok")

    dispatcher, _ = _make_dispatcher(_handler)

    result = await dispatcher.dispatch(ToolCall(id="call_2", name="not_registered", arguments={}))

    assert result.outcome == "error"
    assert result.error_code == "unknown_tool"
    assert result.tool_name == "not_registered"
    assert result.tool_version is None
    assert result.task_id is None

    events = _snapshot_route_events()
    assert len(events) == 1
    assert events[0].payload["outcome"] == "error"
    assert events[0].payload["error_code"] == "unknown_tool"
    assert events[0].severity == "warn"


@pytest.mark.asyncio
async def test_dispatch_invalid_args_returns_validation_error() -> None:
    async def _handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
        raise AssertionError("handler must not be called on validation failure")

    dispatcher, _ = _make_dispatcher(_handler)

    # Missing required field ``goal`` → Pydantic raises → dispatcher
    # converts to invalid_args.
    result = await dispatcher.dispatch(
        ToolCall(id="call_3", name="stub_tool", arguments={"title": "T"})
    )

    assert result.outcome == "error"
    assert result.error_code == "invalid_args"
    assert result.tool_name == "stub_tool"
    assert result.tool_version == "v1"
    assert result.task_id is None
    assert result.error_message is not None
    assert "goal" in result.error_message

    events = _snapshot_route_events()
    assert len(events) == 1
    assert events[0].payload["error_code"] == "invalid_args"


@pytest.mark.asyncio
async def test_dispatch_handler_error_propagates_to_result() -> None:
    """Handler-side domain errors surface through the same code path."""

    async def _handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(
            status="error",
            task_id="task-99",
            error_code="unknown_task",
            error_message="task task-99 not found",
        )

    dispatcher, _ = _make_dispatcher(_handler)

    result = await dispatcher.dispatch(
        ToolCall(id="call_4", name="stub_tool", arguments={"title": "T", "goal": "G"})
    )

    assert result.outcome == "error"
    assert result.error_code == "unknown_task"
    assert result.tool_name == "stub_tool"
    assert result.tool_version == "v1"
    assert result.task_id == "task-99"

    events = _snapshot_route_events()
    assert len(events) == 1
    assert events[0].payload["error_code"] == "unknown_task"
    assert events[0].payload["task_id"] == "task-99"


@pytest.mark.asyncio
async def test_dispatch_handler_exception_surfaces_as_error_result() -> None:
    """A handler that raises produces an error result (not an uncaught exception).

    The dispatcher must keep its no-throw contract so the orchestrator
    code path stays branchless on exception types.
    """

    async def _handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
        raise RuntimeError("boom")

    dispatcher, _ = _make_dispatcher(_handler)

    result = await dispatcher.dispatch(
        ToolCall(id="call_5", name="stub_tool", arguments={"title": "T", "goal": "G"})
    )

    assert result.outcome == "error"
    assert result.error_code == "handler_failed"
    assert result.error_message == "boom"


@pytest.mark.asyncio
async def test_route_event_argument_keys_redact_values() -> None:
    """The route event records argument keys but never values (PII safety)."""

    async def _handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(status="ok")

    dispatcher, _ = _make_dispatcher(_handler)

    await dispatcher.dispatch(
        ToolCall(
            id="call_6",
            name="stub_tool",
            arguments={"title": "secret title", "goal": "secret goal"},
        )
    )

    events = _snapshot_route_events()
    assert len(events) == 1
    payload = events[0].payload
    assert "argument_keys" in payload
    assert sorted(payload["argument_keys"]) == ["goal", "title"]
    # Values must not leak verbatim into the debug payload — keys only.
    flat_payload = repr(payload)
    assert "secret title" not in flat_payload
    assert "secret goal" not in flat_payload


def test_dispatcher_exposes_registry() -> None:
    async def _handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(status="ok")

    dispatcher, definition = _make_dispatcher(_handler)
    assert dispatcher.registry.get("stub_tool") is definition
