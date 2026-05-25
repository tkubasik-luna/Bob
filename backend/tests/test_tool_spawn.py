"""Behavior-preservation tests for ``spawn_subtask`` (v1).

The dispatcher-level happy path is covered in ``test_tool_dispatcher``;
here we pin the side-effect contract of the spawn handler itself:

- creates a row in the task store,
- emits ``task_created`` on the ws emitter,
- calls ``scheduler.enqueue`` exactly once.

Validation behavior (empty title / empty goal) is tested through the
Pydantic ``min_length=1`` constraint at the dispatcher level — the
contract test in ``test_tool_dispatcher.py`` already covers the
invalid-args path; here we focus on the side-effect surface.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.llm.types import ToolCall
from bob.task_store import TaskStore
from bob.tools.definitions.spawn import build_spawn_subtask_tool
from bob.tools.dispatcher import ToolDispatcher, ToolHandlerContext
from bob.tools.registry import ToolRegistry


class _RecordingScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)

    async def resume(self, task_id: str) -> None:
        raise AssertionError("resume not expected on spawn path")

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        raise AssertionError("cancel not expected on spawn path")


@pytest.mark.asyncio
async def test_spawn_creates_task_emits_event_and_enqueues() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler()
    emitted: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    dispatcher = ToolDispatcher(
        registry=ToolRegistry([build_spawn_subtask_tool()]),
        context=ToolHandlerContext(
            task_store=task_store,
            task_scheduler=scheduler,
            ws_emit=_emit,
        ),
    )

    result = await dispatcher.dispatch(
        ToolCall(
            id="call_1",
            name="spawn_subtask",
            arguments={"title": "Drafts", "goal": "Draft 3 emails"},
        )
    )

    assert result.outcome == "ok"
    assert result.tool_name == "spawn_subtask"
    assert result.tool_version == "v1"
    assert result.task_id is not None

    task = task_store.get_task(result.task_id)
    assert task.title == "Drafts"
    assert task.goal == "Draft 3 emails"
    # 0044 baseline: a fresh task carries an empty lineage list.
    assert task.lineage == []

    assert scheduler.enqueued == [result.task_id]
    assert len(emitted) == 1
    assert emitted[0]["type"] == "task_created"
    assert emitted[0]["task_id"] == result.task_id
    assert emitted[0]["title"] == "Drafts"
    assert emitted[0]["goal"] == "Draft 3 emails"


@pytest.mark.asyncio
async def test_spawn_rejects_whitespace_only_title() -> None:
    """The Pydantic ``min_length=1`` constraint rejects whitespace-only titles."""

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler()

    async def _emit(event: dict[str, Any]) -> None:
        raise AssertionError("ws_emit must not fire on validation failure")

    dispatcher = ToolDispatcher(
        registry=ToolRegistry([build_spawn_subtask_tool()]),
        context=ToolHandlerContext(
            task_store=task_store,
            task_scheduler=scheduler,
            ws_emit=_emit,
        ),
    )

    # Empty after strip: Pydantic accepts the string (length>=1), the
    # handler strips and returns the structured invalid_args error.
    result = await dispatcher.dispatch(
        ToolCall(
            id="call_2",
            name="spawn_subtask",
            arguments={"title": "   ", "goal": "Draft 3 emails"},
        )
    )

    assert result.outcome == "error"
    assert result.error_code == "invalid_args"
    assert scheduler.enqueued == []
    assert task_store.list_tasks() == []
