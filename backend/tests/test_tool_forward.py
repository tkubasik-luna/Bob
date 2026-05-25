"""Behavior-preservation tests for ``forward_to_subtask`` (v1).

The handler must:

- append a ``user`` message to the target task's log,
- emit a ``task_message`` ws event,
- call ``scheduler.resume(task_id)`` exactly once,
- return ``status="error"`` (with the right ``error_code``) when the
  target id is unknown or the task is not in ``waiting_input``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.llm.types import ToolCall
from bob.task_store import TaskStore
from bob.tools.definitions.forward import build_forward_to_subtask_tool
from bob.tools.dispatcher import ToolDispatcher, ToolHandlerContext
from bob.tools.registry import ToolRegistry


class _RecordingScheduler:
    def __init__(self) -> None:
        self.resumed: list[str] = []

    async def enqueue(self, task_id: str) -> None:
        raise AssertionError("enqueue not expected on forward path")

    async def resume(self, task_id: str) -> None:
        self.resumed.append(task_id)

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        raise AssertionError("cancel not expected on forward path")


def _make_dispatcher(
    task_store: TaskStore,
    scheduler: _RecordingScheduler,
    emit: Any,
) -> ToolDispatcher:
    return ToolDispatcher(
        registry=ToolRegistry([build_forward_to_subtask_tool()]),
        context=ToolHandlerContext(
            task_store=task_store,
            task_scheduler=scheduler,
            ws_emit=emit,
        ),
    )


def _seed_waiting_task(task_store: TaskStore) -> str:
    """Insert a task and walk it to ``waiting_input`` with one ask_user line."""

    task_id = task_store.create_task(title="Draft email", goal="Write a draft")
    task_store.update_state(task_id, "running")
    task_store.append_message(task_id, role="assistant", content="Quel ton ?", action="ask_user")
    task_store.update_state(task_id, "waiting_input")
    return task_id


@pytest.mark.asyncio
async def test_forward_happy_path_appends_message_and_resumes() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler()
    emitted: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    dispatcher = _make_dispatcher(task_store, scheduler, _emit)
    task_id = _seed_waiting_task(task_store)

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_1",
            name="forward_to_subtask",
            arguments={"task_id": task_id, "response": "Amical."},
        )
    )

    assert result.outcome == "ok"
    assert result.task_id == task_id

    messages = task_store.get_task_messages(task_id)
    assert any(m.role == "user" and m.content == "Amical." for m in messages)
    assert scheduler.resumed == [task_id]
    assert any(e["type"] == "task_message" for e in emitted)


@pytest.mark.asyncio
async def test_forward_unknown_task_returns_unknown_task_error() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler()

    async def _emit(event: dict[str, Any]) -> None:
        raise AssertionError("emit not expected on unknown-task path")

    dispatcher = _make_dispatcher(task_store, scheduler, _emit)

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_2",
            name="forward_to_subtask",
            arguments={"task_id": "does-not-exist", "response": "Hello"},
        )
    )

    assert result.outcome == "error"
    assert result.error_code == "unknown_task"
    assert result.task_id == "does-not-exist"
    assert scheduler.resumed == []


@pytest.mark.asyncio
async def test_forward_wrong_state_returns_task_not_waiting_input_error() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler()

    async def _emit(event: dict[str, Any]) -> None:
        raise AssertionError("emit not expected on wrong-state path")

    dispatcher = _make_dispatcher(task_store, scheduler, _emit)

    # Task is ``pending`` — not ``waiting_input``.
    task_id = task_store.create_task(title="Pending task", goal="Do later")

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_3",
            name="forward_to_subtask",
            arguments={"task_id": task_id, "response": "Hello"},
        )
    )

    assert result.outcome == "error"
    assert result.error_code == "task_not_waiting_input"
    assert result.task_id == task_id
    assert scheduler.resumed == []
