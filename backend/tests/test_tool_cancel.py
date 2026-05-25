"""Behavior-preservation tests for ``cancel_subtask`` (v1).

The scheduler is permissive on cancel (unknown / terminal task → no-op),
so the contract here is simply:

- the handler forwards ``task_id`` + ``reason`` to the scheduler,
- a missing reason falls back to ``"user_cancelled"``,
- the result echoes the target task id.
"""

from __future__ import annotations

from typing import Any

import pytest

from bob.llm.types import ToolCall
from bob.tools.definitions.cancel import build_cancel_subtask_tool
from bob.tools.dispatcher import ToolDispatcher, ToolHandlerContext
from bob.tools.registry import ToolRegistry


class _RecordingScheduler:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str]] = []

    async def enqueue(self, task_id: str) -> None:
        raise AssertionError("enqueue not expected on cancel path")

    async def resume(self, task_id: str) -> None:
        raise AssertionError("resume not expected on cancel path")

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        self.cancelled.append((task_id, reason))


class _StubTaskStore:
    def create_task(
        self,
        *,
        title: str,
        goal: str,
        parent_task_id: str | None = None,
        lineage: Any = None,
    ) -> str:
        raise AssertionError("not used by cancel tool")

    def get_task(self, task_id: str) -> Any:
        raise AssertionError("not used by cancel tool")

    def append_message(
        self,
        task_id: str,
        *,
        role: Any,
        content: str,
        action: Any = None,
    ) -> int:
        raise AssertionError("not used by cancel tool")

    def get_task_messages(self, task_id: str) -> Any:
        raise AssertionError("not used by cancel tool")


def _make_dispatcher(scheduler: _RecordingScheduler) -> ToolDispatcher:
    async def _emit(event: dict[str, Any]) -> None:
        raise AssertionError("cancel must not emit ws events")

    return ToolDispatcher(
        registry=ToolRegistry([build_cancel_subtask_tool()]),
        context=ToolHandlerContext(
            task_store=_StubTaskStore(),
            task_scheduler=scheduler,
            ws_emit=_emit,
        ),
    )


@pytest.mark.asyncio
async def test_cancel_defaults_reason_when_omitted() -> None:
    scheduler = _RecordingScheduler()
    dispatcher = _make_dispatcher(scheduler)

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_1",
            name="cancel_subtask",
            arguments={"task_id": "task-1"},
        )
    )

    assert result.outcome == "ok"
    assert result.task_id == "task-1"
    assert scheduler.cancelled == [("task-1", "user_cancelled")]


@pytest.mark.asyncio
async def test_cancel_forwards_explicit_reason() -> None:
    scheduler = _RecordingScheduler()
    dispatcher = _make_dispatcher(scheduler)

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_2",
            name="cancel_subtask",
            arguments={"task_id": "task-2", "reason": "trop long"},
        )
    )

    assert result.outcome == "ok"
    assert scheduler.cancelled == [("task-2", "trop long")]


@pytest.mark.asyncio
async def test_cancel_whitespace_reason_collapses_to_default() -> None:
    """A whitespace-only reason falls back to ``user_cancelled`` (legacy parity)."""

    scheduler = _RecordingScheduler()
    dispatcher = _make_dispatcher(scheduler)

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_3",
            name="cancel_subtask",
            arguments={"task_id": "task-3", "reason": "   "},
        )
    )

    assert result.outcome == "ok"
    assert scheduler.cancelled == [("task-3", "user_cancelled")]
