"""Tests for :mod:`bob.task_scheduler`.

Strategy: pair a real :class:`TaskStore` (in-memory SQLite + migrations) with
a "controllable" runner factory whose tasks block on caller-owned
:class:`asyncio.Event` instances. The test code releases events one at a
time to observe promotion behaviour deterministically.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Coroutine
from typing import Any

import pytest

from bob import ws_events
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.task_scheduler import TaskScheduler
from bob.task_store import TaskStore


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


class _ControlledRunner:
    """Factory whose runner coroutines block on caller-released events.

    For each enqueued ``task_id`` the factory registers an
    :class:`asyncio.Event` accessible via :meth:`event_for`. The runner
    awaits that event and then transitions the task to ``done`` (so the
    scheduler observes a clean termination via its done-callback).
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        self._events: dict[str, asyncio.Event] = {}
        self.started: list[str] = []

    def event_for(self, task_id: str) -> asyncio.Event:
        return self._events.setdefault(task_id, asyncio.Event())

    def runner_factory(self, task_id: str) -> Coroutine[Any, Any, None]:
        event = self._events.setdefault(task_id, asyncio.Event())

        async def _run() -> None:
            self.started.append(task_id)
            await event.wait()
            # Transition to done (terminal). Tests that want to simulate
            # failure can call ``set_failed`` before releasing the event.
            current = self._store.get_task(task_id)
            if current.state == "running":
                self._store.update_state(task_id, "done")

        return _run()

    async def release(self, task_id: str) -> None:
        """Release the runner, then yield enough to let the scheduler's
        done-callback fire (which schedules ``on_task_terminated`` via
        ``create_task``, so an extra loop tick is needed)."""

        self.event_for(task_id).set()
        # Allow runner to run to completion + done-callback to fire + the
        # follow-up ``on_task_terminated`` coroutine to acquire the lock and
        # promote the next pending task (which itself schedules a new runner
        # via ``create_task``). Three turns cover the chain reliably.
        for _ in range(3):
            await asyncio.sleep(0)


def _create_pending(store: TaskStore, *, title: str = "t", goal: str = "g") -> str:
    return store.create_task(title=title, goal=goal)


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_five_with_cap_three_yields_three_running_two_pending() -> None:
    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    ids = [_create_pending(store, title=f"t{i}", goal=f"g{i}") for i in range(5)]
    for tid in ids:
        await scheduler.enqueue(tid)

    # Let the scheduler's promotion side-effects (state update + WS emit) run.
    await asyncio.sleep(0)

    running = [t.id for t in store.list_tasks(state="running")]
    pending = [t.id for t in store.list_tasks(state="pending")]

    assert running == ids[:3]
    assert pending == ids[3:]
    assert scheduler.running_task_ids() == set(ids[:3])

    # Drain the in-flight runners so the test loop exits cleanly.
    for tid in ids[:3]:
        await runner.release(tid)


@pytest.mark.asyncio
async def test_termination_promotes_oldest_pending() -> None:
    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    ids = [_create_pending(store, title=f"t{i}", goal=f"g{i}") for i in range(5)]
    for tid in ids:
        await scheduler.enqueue(tid)
    await asyncio.sleep(0)

    # Release the first running task; expect ids[3] (oldest pending) promoted.
    await runner.release(ids[0])

    running_now = {t.id for t in store.list_tasks(state="running")}
    pending_now = {t.id for t in store.list_tasks(state="pending")}
    done_now = {t.id for t in store.list_tasks(state="done")}

    assert running_now == {ids[1], ids[2], ids[3]}
    assert pending_now == {ids[4]}
    assert done_now == {ids[0]}

    # Cleanup.
    for tid in (ids[1], ids[2], ids[3]):
        await runner.release(tid)
    await runner.release(ids[4])


@pytest.mark.asyncio
async def test_release_all_drains_queue_to_done() -> None:
    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    ids = [_create_pending(store) for _ in range(5)]
    for tid in ids:
        await scheduler.enqueue(tid)
    await asyncio.sleep(0)

    # Release in arbitrary order; the scheduler must keep the cap honoured
    # as it drains.
    for tid in ids:
        # Some events may not be set up yet (queued items haven't been
        # promoted) — release them as soon as each one transitions to
        # running. Easiest: release-all, then loop until everything done.
        runner.event_for(tid).set()

    # Give the scheduler enough turns to drain the chain. We wait for both
    # state transitions AND the scheduler's in-memory bookkeeping to converge
    # (the bookkeeping update fires from a follow-up coroutine scheduled by
    # the runner's done-callback, so it lags the state transition by a tick).
    for _ in range(40):
        await asyncio.sleep(0)
        states_now = {t.state for t in store.list_tasks()}
        if states_now == {"done"} and not scheduler.running_task_ids():
            break

    states = [t.state for t in store.list_tasks()]
    assert states == ["done"] * 5
    assert scheduler.running_task_ids() == set()


# ---------------------------------------------------------------------------
# Concurrency / lock correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_enqueue_respects_cap() -> None:
    """``asyncio.gather`` enqueues — the lock must serialise promotions."""

    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    ids = [_create_pending(store) for _ in range(5)]
    await asyncio.gather(*(scheduler.enqueue(tid) for tid in ids))
    await asyncio.sleep(0)

    running = [t.id for t in store.list_tasks(state="running")]
    pending = [t.id for t in store.list_tasks(state="pending")]
    assert len(running) == 3
    assert len(pending) == 2
    assert set(running) | set(pending) == set(ids)

    for tid in running:
        await runner.release(tid)


# ---------------------------------------------------------------------------
# Boot-time recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_after_restart_coerces_running_and_re_promotes() -> None:
    """Seed rows in state=running (simulating a crashed previous process),
    then call ``recover_after_restart``: every stale row must be coerced back
    to ``pending`` and ``cap`` of them promoted."""

    store = _make_store()
    # Pre-seed 4 tasks in state=running directly via raw SQL — the public
    # API forbids creating a task in any state other than ``pending``.
    seeded: list[str] = []
    for i in range(4):
        tid = store.create_task(title=f"crashed{i}", goal=f"g{i}")
        # Force transition to running by going through the normal validator;
        # this is a legal pending → running step.
        store.update_state(tid, "running")
        seeded.append(tid)

    # Sanity: 4 in running.
    assert {t.id for t in store.list_tasks(state="running")} == set(seeded)

    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    await scheduler.recover_after_restart()
    await asyncio.sleep(0)

    running_after = [t.id for t in store.list_tasks(state="running")]
    pending_after = [t.id for t in store.list_tasks(state="pending")]
    # Cap=3 → exactly 3 re-promoted in creation order; remaining one queued.
    assert running_after == seeded[:3]
    assert pending_after == [seeded[3]]
    assert scheduler.running_task_ids() == set(seeded[:3])

    for tid in seeded[:3]:
        await runner.release(tid)


@pytest.mark.asyncio
async def test_recover_after_restart_with_no_running_is_noop() -> None:
    """No rows in running → recovery should still re-enqueue any existing pending."""

    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    # Two pending tasks survived a clean restart.
    a = _create_pending(store, title="a")
    b = _create_pending(store, title="b")

    await scheduler.recover_after_restart()
    await asyncio.sleep(0)

    running_after = {t.id for t in store.list_tasks(state="running")}
    assert running_after == {a, b}

    for tid in (a, b):
        await runner.release(tid)


# ---------------------------------------------------------------------------
# WS emit on promotion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promotion_after_termination_emits_task_updated_running() -> None:
    """When ``on_task_terminated`` promotes a pending row, a ``task_updated``
    event with ``state=running`` must reach the WS emitter."""

    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=1, runner_factory=runner.runner_factory)

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        first = _create_pending(store, title="first")
        second = _create_pending(store, title="second")
        await scheduler.enqueue(first)
        await scheduler.enqueue(second)
        await asyncio.sleep(0)

        # After initial enqueue: ``first`` got task_updated → running.
        assert any(
            e.get("type") == "task_updated"
            and e.get("task_id") == first
            and e.get("state") == "running"
            for e in received
        )
        # ``second`` is still pending — no task_updated yet.
        assert not any(
            e.get("type") == "task_updated" and e.get("task_id") == second for e in received
        )

        # Release the first runner; expect a task_updated for ``second``.
        await runner.release(first)

        promotions = [
            e
            for e in received
            if e.get("type") == "task_updated"
            and e.get("task_id") == second
            and e.get("state") == "running"
        ]
        assert len(promotions) == 1
        event = promotions[0]
        assert event["needs_attention"] is False
        assert isinstance(event["updated_at"], str)
    finally:
        ws_events.set_emitter(None)

    await runner.release(second)


# ---------------------------------------------------------------------------
# Misc edge cases
# ---------------------------------------------------------------------------


def test_cap_must_be_positive() -> None:
    store = _make_store()
    with pytest.raises(ValueError):
        TaskScheduler(
            task_store=store,
            cap=0,
            runner_factory=_ControlledRunner(store).runner_factory,
        )


# ---------------------------------------------------------------------------
# resume() — slice #0021 forward_to_subtask handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_promotes_waiting_input_to_running() -> None:
    """``resume`` must transition waiting_input → running and schedule a runner."""

    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    # Seed a task directly in waiting_input via the legal pending→running→waiting_input
    # transition chain.
    tid = _create_pending(store, title="paused")
    store.update_state(tid, "running")
    store.update_state(tid, "waiting_input")

    await scheduler.resume(tid)
    await asyncio.sleep(0)

    assert store.get_task(tid).state == "running"
    assert scheduler.running_task_ids() == {tid}
    assert tid in runner.started

    await runner.release(tid)


@pytest.mark.asyncio
async def test_resume_no_slot_leaves_task_waiting_input() -> None:
    """When cap is saturated, ``resume`` must leave the task unchanged."""

    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=1, runner_factory=runner.runner_factory)

    # Fill the cap with one running task.
    blocking = _create_pending(store, title="blocker")
    await scheduler.enqueue(blocking)
    await asyncio.sleep(0)
    assert store.get_task(blocking).state == "running"

    # Now create a paused task and try to resume it — cap is full.
    paused = _create_pending(store, title="paused")
    store.update_state(paused, "running")
    store.update_state(paused, "waiting_input")
    # First we must free the blocking task from running so we can re-add via
    # the cap-saturated path. Actually we want cap saturated so leave blocker
    # running, but the slot is taken — resume should drop.
    await scheduler.resume(paused)
    await asyncio.sleep(0)

    assert store.get_task(paused).state == "waiting_input"
    assert scheduler.running_task_ids() == {blocking}

    await runner.release(blocking)


@pytest.mark.asyncio
async def test_resume_wrong_state_is_dropped() -> None:
    """``resume`` on a task that isn't in waiting_input is a no-op."""

    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    # Pending task — resume should refuse.
    pending = _create_pending(store, title="pending")
    await scheduler.resume(pending)
    await asyncio.sleep(0)

    assert store.get_task(pending).state == "pending"
    assert scheduler.running_task_ids() == set()
    assert pending not in runner.started


@pytest.mark.asyncio
async def test_resume_unknown_task_is_dropped() -> None:
    store = _make_store()
    runner = _ControlledRunner(store)
    scheduler = TaskScheduler(task_store=store, cap=3, runner_factory=runner.runner_factory)

    await scheduler.resume("nonexistent-task-id")
    await asyncio.sleep(0)

    assert scheduler.running_task_ids() == set()
