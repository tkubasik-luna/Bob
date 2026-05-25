"""Tests for :mod:`bob.event_bus_v2` — unified event producer (issue 0052).

Covers:
- ``emit_event`` lands a :class:`DebugEvent` in the ring buffer AND forwards
  the WS payload to the registered emitter (no parallel paths);
- ``task_id`` field is populated from the ``current_task_id`` ContextVar OR
  from the WS payload's ``task_id`` key when no ContextVar is set;
- ``get_snapshot_for_task`` returns only events tagged with the requested id;
- ``subscribe_for_task`` yields snapshot-then-tail filtered by ``task_id``,
  with no cross-leak between two concurrent overlays;
- the legacy :mod:`bob.ws_events` ``emit``/``set_emitter`` shim routes
  through the bus so no producer needs to call both paths separately
  (collapse assertion).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Iterator
from typing import Any

import pytest

from bob import debug_log, event_bus_v2, ws_events
from bob.debug_log import (
    clear,
    current_task_id,
    current_turn_id,
    emit_debug,
    start_task,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    event_bus_v2.set_ws_emitter(None)
    yield
    clear()
    debug_log._subscribers.clear()
    current_turn_id.set(None)
    current_task_id.set(None)
    event_bus_v2.set_ws_emitter(None)


@pytest.mark.asyncio
async def test_emit_event_lands_in_ring_buffer_and_forwards_to_ws() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    event_bus_v2.set_ws_emitter(_emitter)

    payload = {"type": "task_updated", "task_id": "task-A", "state": "running"}
    await event_bus_v2.emit_event(payload)

    assert received == [payload]
    snapshot = event_bus_v2.get_snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].payload == {"ws_event": payload}
    # The event must be filterable per task_id even though the producer
    # runs outside a ContextVar scope.
    assert snapshot[0].task_id == "task-A"


@pytest.mark.asyncio
async def test_emit_event_uses_context_var_when_payload_has_no_task_id() -> None:
    payload = {"type": "task_message", "message_id": 42}
    token = start_task("task-X")
    try:
        await event_bus_v2.emit_event(payload)
    finally:
        current_task_id.reset(token)

    [event] = event_bus_v2.get_snapshot()
    assert event.task_id == "task-X"
    assert event.parent_task_id == "task-X"


@pytest.mark.asyncio
async def test_legacy_ws_events_emit_routes_through_unified_bus() -> None:
    """Producers that called ``ws_events.emit`` MUST land in the ring buffer.

    This is the grep-cleaned assertion: no module should need to call both
    ``ws_events.emit`` AND ``emit_debug`` separately to populate the two
    channels. After issue 0052 the legacy ``ws_events.emit`` is a shim
    routing through :func:`bob.event_bus_v2.emit_event`.
    """

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    await ws_events.emit({"type": "task_created", "task_id": "task-K"})

    # WS chat client got the payload exactly as before.
    assert received == [{"type": "task_created", "task_id": "task-K"}]
    # AND the unified ring buffer got an entry for the same event — no
    # parallel path needed.
    snapshot = event_bus_v2.get_snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].task_id == "task-K"


@pytest.mark.asyncio
async def test_collapsed_emission_paths_are_singular() -> None:
    """No backend module calls both ``ws_events.emit`` and ``emit_debug``
    in a way that would produce two ring-buffer entries for the same event.

    Verified by inspecting the shim: :func:`bob.ws_events.emit` resolves
    to a single call into :func:`bob.event_bus_v2.emit_event`.
    """

    source = inspect.getsource(ws_events.emit)
    # The shim must not call ``emit_debug`` directly — its only path is
    # through ``emit_event`` (which itself calls ``emit_debug`` once).
    assert "emit_debug" not in source
    # And it must delegate to the unified bus.
    assert "emit_event" in source


def test_get_snapshot_for_task_filters_by_id() -> None:
    token_a = start_task("task-A")
    try:
        emit_debug(category="task", severity="info", source="t", summary="A1")
    finally:
        current_task_id.reset(token_a)

    token_b = start_task("task-B")
    try:
        emit_debug(category="task", severity="info", source="t", summary="B1")
        emit_debug(category="task", severity="info", source="t", summary="B2")
    finally:
        current_task_id.reset(token_b)

    a_events = event_bus_v2.get_snapshot_for_task("task-A")
    b_events = event_bus_v2.get_snapshot_for_task("task-B")
    c_events = event_bus_v2.get_snapshot_for_task("task-C")

    assert [e.summary for e in a_events] == ["A1"]
    assert [e.summary for e in b_events] == ["B1", "B2"]
    assert c_events == []


@pytest.mark.asyncio
async def test_subscribe_for_task_yields_snapshot_then_tail() -> None:
    # Seed a snapshot entry for task-A.
    token_a = start_task("task-A")
    try:
        emit_debug(category="task", severity="info", source="t", summary="seed-A")
    finally:
        current_task_id.reset(token_a)

    received: list[Any] = []
    stopped = asyncio.Event()

    async def _consume() -> None:
        async for event in event_bus_v2.subscribe_for_task("task-A"):
            received.append(event)
            if len(received) >= 2:
                stopped.set()
                return

    consumer = asyncio.create_task(_consume())
    # Yield the loop so the consumer drains the snapshot.
    await asyncio.sleep(0)

    # Live emit for task-A.
    token_a2 = start_task("task-A")
    try:
        emit_debug(category="task", severity="info", source="t", summary="live-A")
    finally:
        current_task_id.reset(token_a2)

    await asyncio.wait_for(stopped.wait(), timeout=1.0)
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer

    summaries = [e.summary for e in received]
    assert summaries == ["seed-A", "live-A"]
    # The first (snapshot) event is tagged replayed; the live one isn't.
    assert received[0].replayed is True
    assert received[1].replayed is False


@pytest.mark.asyncio
async def test_two_subscribers_do_not_cross_leak() -> None:
    """Two concurrent per-task subscriptions must not receive each other's events."""

    received_a: list[str] = []
    received_b: list[str] = []
    stop_a = asyncio.Event()
    stop_b = asyncio.Event()

    async def _consume(task_id: str, sink: list[str], stop: asyncio.Event) -> None:
        async for event in event_bus_v2.subscribe_for_task(task_id):
            sink.append(event.summary)
            # Stop after we've seen the expected count.
            if len(sink) >= 2:
                stop.set()
                return

    consumer_a = asyncio.create_task(_consume("task-A", received_a, stop_a))
    consumer_b = asyncio.create_task(_consume("task-B", received_b, stop_b))
    await asyncio.sleep(0)

    # Interleave emits for A and B.
    for tid, summary in [
        ("task-A", "A1"),
        ("task-B", "B1"),
        ("task-A", "A2"),
        ("task-B", "B2"),
    ]:
        token = start_task(tid)
        try:
            emit_debug(category="task", severity="info", source="t", summary=summary)
        finally:
            current_task_id.reset(token)

    await asyncio.wait_for(stop_a.wait(), timeout=1.0)
    await asyncio.wait_for(stop_b.wait(), timeout=1.0)
    consumer_a.cancel()
    consumer_b.cancel()
    for c in (consumer_a, consumer_b):
        with contextlib.suppress(asyncio.CancelledError):
            await c

    assert received_a == ["A1", "A2"]
    assert received_b == ["B1", "B2"]


@pytest.mark.asyncio
async def test_process_restart_loses_ring_buffer_reflections() -> None:
    """Simulate a process restart: events emitted before are NOT recovered.

    The contract is "reflections stay ring-buffer-only — sub-agents that die
    on process restart take their thoughts with them by design". We assert
    this by clearing the buffer (the equivalent of a fresh process) and
    confirming the per-task snapshot is empty afterwards.
    """

    token = start_task("task-Z")
    try:
        emit_debug(category="task", severity="info", source="t", summary="pre-restart")
    finally:
        current_task_id.reset(token)

    assert event_bus_v2.get_snapshot_for_task("task-Z") != []

    # Simulate restart: a fresh process starts with an empty ring buffer.
    clear()

    assert event_bus_v2.get_snapshot_for_task("task-Z") == []
