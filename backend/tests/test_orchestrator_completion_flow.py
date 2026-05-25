"""Integration tests for the v2 completion flow (PRD 0006 / issue 0050).

Covers the orchestrator-level wiring:

* ``enqueue_completion`` schedules a flush.
* The flush sets ``delivered_at_turn`` and synthesises a
  ``task_completed`` row so the same result is never announced twice.
* Multiple completions within the debounce window batch into a single
  flush.
* The orchestrator's ``user_turn_index`` counter increments on every
  ``process_user_message`` call.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import LLMClient
from bob.orchestrator import Orchestrator
from bob.task_store import TaskStore


class _FakeClient(LLMClient):
    def __init__(self, *, complete: list[LLMResponse]) -> None:
        self._complete = list(complete)
        self.chat_calls: list[Any] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.chat_calls.append(messages)
        return "synthèse fake"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        return self._complete.pop(0)


class _RecScheduler:
    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store
        self.enqueued: list[str] = []
        self.cancelled: list[tuple[str, str]] = []

    async def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)
        with contextlib.suppress(Exception):  # pragma: no cover — defensive net
            self._task_store.update_state(task_id, "running")

    async def resume(self, task_id: str) -> None:
        pass

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        self.cancelled.append((task_id, reason))


@pytest.fixture
def make_orchestrator() -> Any:
    """Build an :class:`Orchestrator` with a tiny debounce window."""

    def _build() -> tuple[Orchestrator, TaskStore]:
        client = _FakeClient(
            complete=[
                LLMResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id="c",
                            name="say",
                            arguments={"speech": "ok"},
                        )
                    ],
                )
            ]
        )
        conn = sqlite3.connect(":memory:")
        apply_migrations(conn, default_migrations_dir())
        js = JarvisStore(conn)
        ts = TaskStore(conn)
        sched = _RecScheduler(ts)
        orch = Orchestrator(
            jarvis_client=client,
            jarvis_store=js,
            task_store=ts,
            task_scheduler=sched,
            jarvis_prompt="Tu es Jarvis-de-test.",
            completion_debounce_seconds=0.0,
        )
        return orch, ts

    return _build


@pytest.mark.asyncio
async def test_user_turn_index_increments_on_process(make_orchestrator: Any) -> None:
    orch, _ = make_orchestrator()
    assert orch.user_turn_index == 0
    await orch.process_user_message("s1", "salut")
    assert orch.user_turn_index == 1


@pytest.mark.asyncio
async def test_enqueue_completion_sets_delivered_at_turn(
    make_orchestrator: Any,
) -> None:
    orch, ts = make_orchestrator()
    # Spawn a task + mark it done so the debouncer flush has something
    # to deliver.
    task_id = ts.create_task(title="t", goal="g")
    ts.update_state(task_id, "running")
    ts.update_state(task_id, "done")
    ts.set_result(task_id, "résultat")

    # The completion debouncer needs a running loop already; we hijack
    # the orchestrator's exposed handle.
    await orch.enqueue_completion(task_id)
    await orch.completion_debouncer.flush_now()
    task = ts.get_task(task_id)
    assert task.delivered_at_turn == orch.user_turn_index


@pytest.mark.asyncio
async def test_enqueue_completion_batches_multiple_ids(make_orchestrator: Any) -> None:
    """Within the debounce window N completions land in one flush call."""

    orch, ts = make_orchestrator()
    ids = []
    for _ in range(3):
        t = ts.create_task(title="t", goal="g")
        ts.update_state(t, "running")
        ts.update_state(t, "done")
        ts.set_result(t, "r")
        ids.append(t)

    flushed_batches: list[list[str]] = []
    real_callback = orch._on_completion_batch

    async def _spy(batch: list[str]) -> None:
        flushed_batches.append(list(batch))
        await real_callback(batch)

    orch._completion_debouncer = type(orch._completion_debouncer)(
        flush_callback=_spy,
        window_seconds=0.0,
    )
    for tid in ids:
        await orch.enqueue_completion(tid)
    await orch.completion_debouncer.flush_now()
    assert flushed_batches == [ids]
    for tid in ids:
        assert ts.get_task(tid).delivered_at_turn == orch.user_turn_index


@pytest.mark.asyncio
async def test_delivery_does_not_double_announce(make_orchestrator: Any) -> None:
    """Once a task has been delivered we may re-flush without re-stamping at a different turn."""

    orch, ts = make_orchestrator()
    task_id = ts.create_task(title="t", goal="g")
    ts.update_state(task_id, "running")
    ts.update_state(task_id, "done")
    ts.set_result(task_id, "résultat")

    await orch.enqueue_completion(task_id)
    await orch.completion_debouncer.flush_now()
    first_turn = ts.get_task(task_id).delivered_at_turn

    # Same task again at the same turn → ``delivered_at_turn`` stays
    # the same.
    await orch.enqueue_completion(task_id)
    await orch.completion_debouncer.flush_now()
    assert ts.get_task(task_id).delivered_at_turn == first_turn


@pytest.mark.asyncio
async def test_set_addendum_queue_factory_late_binding(make_orchestrator: Any) -> None:
    """``set_addendum_queue_factory`` re-wires the dispatcher context."""

    orch, _ = make_orchestrator()
    calls: list[str] = []

    def _factory(task_id: str) -> Any:
        calls.append(task_id)
        return None

    orch.set_addendum_queue_factory(_factory)
    # The dispatcher context now carries the factory — exercise the
    # downstream tool path implicitly by checking the attribute on
    # the orchestrator's stored handle.
    ctx = orch._tool_dispatcher._context
    assert ctx.addendum_queue_factory is _factory
    assert calls == []  # factory not invoked merely by being wired
