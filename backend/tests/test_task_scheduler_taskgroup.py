"""Integration tests for the shared TaskGroup path in :class:`bob.task_scheduler.TaskScheduler`.

PRD 0006 / issue 0045 wraps every running sub-agent in one shared
:class:`asyncio.TaskGroup` so an orchestrator crash propagating up to
the FastAPI lifespan can drain in-flight runners deterministically.

These tests exercise the boot-path wiring: ``start()`` brings up the
group, ``stop()`` cancels every in-flight runner and drains the host
coroutine cleanly. The cooperative-cancel hook + 2 s grace are also
covered.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.task_scheduler import TaskScheduler
from bob.task_store import TaskStore


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _create_pending(store: TaskStore, *, title: str = "t", goal: str = "g") -> str:
    return store.create_task(title=title, goal=goal)


@pytest.mark.asyncio
async def test_start_stop_drains_in_flight_runners() -> None:
    """``stop()`` cancels every running task and the host coroutine returns."""

    store = _make_store()
    running_started: list[str] = []
    cancelled_flags: list[str] = []

    def runner_factory(task_id: str) -> Coroutine[Any, Any, None]:
        async def _run() -> None:
            running_started.append(task_id)
            try:
                # Block forever — only ``stop()`` should pull us out.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_flags.append(task_id)
                # Persist a terminal state so the row leaves ``running``
                # (otherwise the next test seeing a stale row would
                # mis-recover).
                store.set_result(task_id, "cancelled")
                store.update_state(task_id, "failed")
                raise

        return _run()

    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner_factory)
    await scheduler.start()

    ids = [_create_pending(store, title=f"t{i}") for i in range(3)]
    for tid in ids:
        await scheduler.enqueue(tid)
    # Let the runners reach their ``await`` point.
    for _ in range(3):
        await asyncio.sleep(0)
    assert sorted(running_started) == sorted(ids)

    await scheduler.stop()

    # Every runner observed CancelledError thanks to the TaskGroup
    # tearing down its children deterministically.
    assert sorted(cancelled_flags) == sorted(ids)
    # And the in-memory set is back to empty.
    assert scheduler.running_task_ids() == set()


@pytest.mark.asyncio
async def test_cooperative_cancel_runs_before_hard_kill() -> None:
    """The coop hook fires first; only past the grace does the hard kill land."""

    store = _make_store()
    coop_calls: list[str] = []
    cancelled_flags: list[str] = []
    finish_events: dict[str, asyncio.Event] = {}

    def runner_factory(task_id: str) -> Coroutine[Any, Any, None]:
        async def _run() -> None:
            ev = finish_events.setdefault(task_id, asyncio.Event())
            try:
                await ev.wait()
                # Terminal transition matches what SubAgentRunner does on a
                # cooperative cancel — the row lands in ``failed``.
                store.update_state(task_id, "failed")
            except asyncio.CancelledError:
                cancelled_flags.append(task_id)
                raise

        return _run()

    def coop_cancel_factory(task_id: str) -> Callable[[], None] | None:
        def _request() -> None:
            coop_calls.append(task_id)
            # Simulate the runner observing the flag and exiting cleanly
            # within the grace window.
            finish_events.setdefault(task_id, asyncio.Event()).set()

        return _request

    scheduler = TaskScheduler(
        task_store=store,
        cap=1,
        runner_factory=runner_factory,
        coop_cancel_factory=coop_cancel_factory,
        cancel_grace_seconds=0.5,
    )
    await scheduler.start()
    try:
        tid = _create_pending(store, title="target")
        await scheduler.enqueue(tid)
        for _ in range(3):
            await asyncio.sleep(0)

        await scheduler.cancel(tid, reason="user_cancelled")

        # Cooperative hook fired exactly once.
        assert coop_calls == [tid]
        # Hard kill did NOT fire because the runner finished within the
        # 0.5 s grace.
        assert cancelled_flags == []
        # State settled to failed.
        assert store.get_task(tid).state == "failed"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_cooperative_cancel_grace_elapsed_escalates_to_hard_kill() -> None:
    """When the runner ignores the coop flag, the grace elapses → hard kill."""

    store = _make_store()
    cancelled_flags: list[str] = []

    def runner_factory(task_id: str) -> Coroutine[Any, Any, None]:
        async def _run() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_flags.append(task_id)
                store.update_state(task_id, "failed")
                raise

        return _run()

    def coop_cancel_factory(_task_id: str) -> Callable[[], None] | None:
        # Hook is registered but is a no-op — simulating a misbehaving
        # runner that doesn't honour the cooperative flag.
        return lambda: None

    scheduler = TaskScheduler(
        task_store=store,
        cap=1,
        runner_factory=runner_factory,
        coop_cancel_factory=coop_cancel_factory,
        cancel_grace_seconds=0.01,
    )
    await scheduler.start()
    try:
        tid = _create_pending(store, title="target")
        await scheduler.enqueue(tid)
        for _ in range(3):
            await asyncio.sleep(0)

        await scheduler.cancel(tid)

        # The grace elapsed → hard kill fired → runner saw CancelledError.
        assert cancelled_flags == [tid]
        assert store.get_task(tid).state == "failed"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    """Calling start twice is a safe no-op (boot path resilience)."""

    store = _make_store()

    def runner_factory(_task_id: str) -> Coroutine[Any, Any, None]:
        async def _run() -> None:
            await asyncio.Event().wait()

        return _run()

    scheduler = TaskScheduler(task_store=store, cap=1, runner_factory=runner_factory)
    await scheduler.start()
    await scheduler.start()  # second call is a no-op
    await scheduler.stop()
