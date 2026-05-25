"""Tests for the v2 task tools shipped in PRD 0006 / issue 0050.

* :class:`SpawnTaskTool`  — replaces the legacy ``spawn_subtask``.
* :class:`AddendumTaskTool` — pushes info into a running
  :class:`AddendumQueue` without restarting the runner.
* :class:`ReplanTaskTool`   — cancel + respawn with ``lineage``;
  marks the old task ``superseded``.
* :class:`CancelTaskTool`   — routes to the scheduler with the
  ``user_cancelled`` default reason.

The tests exercise each handler through the live :class:`ToolDispatcher`
so the contract (validation, error codes, route events) is end-to-end.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any, cast

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.llm.types import ToolCall
from bob.scheduler_policy import SchedulerPolicy
from bob.sub_agent.addendum_queue import AddendumQueue
from bob.task_scheduler import TaskScheduler
from bob.task_store import TaskStore
from bob.tools.definitions.addendum_task import build_addendum_task_tool
from bob.tools.definitions.cancel_task import build_cancel_task_tool
from bob.tools.definitions.replan_task import build_replan_task_tool
from bob.tools.definitions.spawn_task import build_spawn_task_tool
from bob.tools.dispatcher import ToolDispatcher, ToolHandlerContext
from bob.tools.registry import ToolRegistry


def _setup_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


class _RecordingScheduler:
    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store
        self.enqueued: list[str] = []
        self.resumed: list[str] = []
        self.cancelled: list[tuple[str, str]] = []
        self.fail_enqueue_with: BaseException | None = None

    async def enqueue(self, task_id: str) -> None:
        if self.fail_enqueue_with is not None:
            raise self.fail_enqueue_with
        self.enqueued.append(task_id)
        # Move to ``running`` so subsequent ``addendum_task`` finds
        # the task in the right state.
        with contextlib.suppress(Exception):  # pragma: no cover — defensive net
            self._task_store.update_state(task_id, "running")

    async def resume(self, task_id: str) -> None:
        self.resumed.append(task_id)

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        # Mirror real scheduler behaviour: transition the row to
        # ``failed`` so the subsequent ``mark_superseded`` test path
        # sees a non-terminal source.
        self.cancelled.append((task_id, reason))
        with contextlib.suppress(Exception):  # pragma: no cover — defensive net
            current = self._task_store.get_task(task_id)
            if current.state in (
                "spawned",
                "pending",
                "running",
                "awaiting_input",
                "waiting_input",
            ):
                if current.state in ("running",):
                    self._task_store.set_result(task_id, reason)
                self._task_store.update_state(task_id, "failed")


async def _noop_emit(event: dict[str, Any]) -> None:
    return None


def _make_dispatcher(
    *,
    task_store: TaskStore,
    scheduler: _RecordingScheduler,
    addendum_queue_factory: Any | None = None,
) -> ToolDispatcher:
    registry = ToolRegistry(
        [
            build_spawn_task_tool(),
            build_addendum_task_tool(),
            build_replan_task_tool(),
            build_cancel_task_tool(),
        ]
    )
    return ToolDispatcher(
        registry=registry,
        context=ToolHandlerContext(
            task_store=task_store,
            task_scheduler=scheduler,
            ws_emit=_noop_emit,
            addendum_queue_factory=addendum_queue_factory,
            mark_superseded=task_store.mark_superseded,
        ),
    )


# --- spawn_task ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_task_creates_row_and_enqueues() -> None:
    store = _setup_store()
    scheduler = _RecordingScheduler(store)
    dispatcher = _make_dispatcher(task_store=store, scheduler=scheduler)

    result = await dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="spawn_task",
            arguments={"title": "Brief", "goal": "Synthétise les emails"},
        )
    )

    assert result.ok
    assert result.tool_name == "spawn_task"
    assert result.task_id is not None
    assert scheduler.enqueued == [result.task_id]
    task = store.get_task(result.task_id)
    # Scheduler recording double promoted to running.
    assert task.state == "running"


@pytest.mark.asyncio
async def test_spawn_task_surfaces_queue_full() -> None:
    from bob.scheduler_policy import SchedulerQueueFull

    store = _setup_store()
    scheduler = _RecordingScheduler(store)
    scheduler.fail_enqueue_with = SchedulerQueueFull(
        running=3, queued=5, max_running=3, max_queued=5
    )
    dispatcher = _make_dispatcher(task_store=store, scheduler=scheduler)

    result = await dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="spawn_task",
            arguments={"title": "Brief", "goal": "x"},
        )
    )

    assert result.ok is False
    assert result.error_code == "scheduler_queue_full"
    task = store.get_task(result.task_id or "")
    assert task.state == "failed"


# --- addendum_task ------------------------------------------------------------


@pytest.mark.asyncio
async def test_addendum_pushes_into_queue_without_restart() -> None:
    """PRD acceptance: addendum visible at iteration boundary, no restart."""

    store = _setup_store()
    scheduler = _RecordingScheduler(store)
    queue = AddendumQueue()
    factory_calls: list[str] = []

    def _factory(task_id: str) -> AddendumQueue | None:
        factory_calls.append(task_id)
        return queue

    dispatcher = _make_dispatcher(
        task_store=store,
        scheduler=scheduler,
        addendum_queue_factory=_factory,
    )
    # Spawn + transition to running.
    spawn = await dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="spawn_task",
            arguments={"title": "Live", "goal": "g"},
        )
    )
    assert spawn.ok and spawn.task_id is not None

    # Push an addendum and assert no restart happened.
    state_before = store.get_task(spawn.task_id).state
    addendum = await dispatcher.dispatch(
        ToolCall(
            id="c2",
            name="addendum_task",
            arguments={"task_id": spawn.task_id, "info": "ajoute X"},
        )
    )
    state_after = store.get_task(spawn.task_id).state

    assert addendum.ok
    assert addendum.task_id == spawn.task_id
    assert state_before == state_after == "running"
    # Drain the queue and assert the addendum landed.
    drained = queue.drain()
    assert [e.text for e in drained] == ["ajoute X"]
    # The factory was invoked with the right id.
    assert factory_calls[-1] == spawn.task_id


@pytest.mark.asyncio
async def test_addendum_rejects_non_running_task() -> None:
    store = _setup_store()
    scheduler = _RecordingScheduler(store)
    queue = AddendumQueue()
    dispatcher = _make_dispatcher(
        task_store=store,
        scheduler=scheduler,
        addendum_queue_factory=lambda _id: queue,
    )

    # Create a task and leave it in ``spawned``.
    task_id = store.create_task(title="t", goal="g")
    store.update_state(task_id, "spawned")
    result = await dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="addendum_task",
            arguments={"task_id": task_id, "info": "x"},
        )
    )
    assert not result.ok
    assert result.error_code == "task_not_running"


@pytest.mark.asyncio
async def test_addendum_unknown_task_returns_unknown_task() -> None:
    store = _setup_store()
    scheduler = _RecordingScheduler(store)
    dispatcher = _make_dispatcher(
        task_store=store,
        scheduler=scheduler,
        addendum_queue_factory=lambda _id: None,
    )
    result = await dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="addendum_task",
            arguments={"task_id": "nope", "info": "x"},
        )
    )
    assert not result.ok
    assert result.error_code == "unknown_task"


# --- replan_task --------------------------------------------------------------


@pytest.mark.asyncio
async def test_replan_chains_lineage_and_marks_superseded() -> None:
    store = _setup_store()
    scheduler = _RecordingScheduler(store)
    dispatcher = _make_dispatcher(task_store=store, scheduler=scheduler)

    spawn = await dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="spawn_task",
            arguments={"title": "Plan A", "goal": "version initiale"},
        )
    )
    assert spawn.ok and spawn.task_id is not None
    old_id = spawn.task_id

    replan = await dispatcher.dispatch(
        ToolCall(
            id="c2",
            name="replan_task",
            arguments={"task_id": old_id, "new_goal": "version finale"},
        )
    )
    assert replan.ok
    new_id = replan.task_id
    assert new_id is not None and new_id != old_id

    old_task = store.get_task(old_id)
    new_task = store.get_task(new_id)
    assert old_task.state == "superseded"
    assert new_task.lineage[0] == old_id


# --- cancel_task --------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_task_routes_to_scheduler_with_default_reason() -> None:
    store = _setup_store()
    scheduler = _RecordingScheduler(store)
    dispatcher = _make_dispatcher(task_store=store, scheduler=scheduler)

    spawn = await dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="spawn_task",
            arguments={"title": "t", "goal": "g"},
        )
    )
    assert spawn.ok
    cancel = await dispatcher.dispatch(
        ToolCall(
            id="c2",
            name="cancel_task",
            arguments={"task_id": spawn.task_id},
        )
    )
    assert cancel.ok
    assert (spawn.task_id, "user_cancelled") in scheduler.cancelled


# --- queue overflow → tool error ---------------------------------------------


@pytest.mark.asyncio
async def test_real_scheduler_queue_overflow_surfaces_error() -> None:
    """End-to-end: real :class:`TaskScheduler` with ``max_queued=0``.

    With ``max_running=1`` + ``max_queued=0`` a second spawn must
    surface ``scheduler_queue_full`` so Jarvis can degrade. We hold the
    first task running by leaking the runner factory's coroutine.
    """

    import asyncio
    from collections.abc import Coroutine

    store = _setup_store()

    started = asyncio.Event()
    block = asyncio.Event()

    def _factory(task_id: str) -> Coroutine[Any, Any, None]:
        async def _run() -> None:
            started.set()
            await block.wait()
            store.update_state(task_id, "done")

        return _run()

    policy = SchedulerPolicy(max_running=1, max_queued=0)
    scheduler = TaskScheduler(
        task_store=store,
        cap=1,
        runner_factory=_factory,
        policy=policy,
    )
    dispatcher = _make_dispatcher(task_store=store, scheduler=cast("Any", scheduler))
    try:
        first = await dispatcher.dispatch(
            ToolCall(
                id="c1",
                name="spawn_task",
                arguments={"title": "first", "goal": "g"},
            )
        )
        assert first.ok
        await started.wait()
        # Second spawn — cap is full, queue size 0 → must overflow.
        second = await dispatcher.dispatch(
            ToolCall(
                id="c2",
                name="spawn_task",
                arguments={"title": "second", "goal": "g"},
            )
        )
        assert not second.ok
        assert second.error_code == "scheduler_queue_full"
    finally:
        block.set()
        # Give the runner a tick to finish and the scheduler's
        # done-callback to wind down before the test exits.
        for _ in range(5):
            await asyncio.sleep(0)
