"""Tests for :mod:`bob.debug_log`.

Covers the deep / pure layer: envelope shape, ring buffer, snapshot,
subscribe iterator (snapshot + live), non-blocking overflow strategy.
The WS surface is tested separately in ``test_ws_debug.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from bob import debug_log
from bob.debug_log import (
    DebugEvent,
    clear,
    emit_debug,
    snapshot,
    subscribe,
    subscriber_count,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Reset module-level state before and after each test."""

    clear()
    debug_log._subscribers.clear()
    yield
    clear()
    debug_log._subscribers.clear()


def test_emit_debug_appends_to_ring_buffer() -> None:
    emit_debug(
        category="input",
        severity="info",
        source="test.emit",
        summary='User envoie: "hello"',
        payload={"content": "hello"},
    )

    events = snapshot()
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, DebugEvent)
    assert event.category == "input"
    assert event.severity == "info"
    assert event.source == "test.emit"
    assert event.summary == 'User envoie: "hello"'
    assert event.payload == {"content": "hello"}
    assert event.turn_id is None
    assert event.correlation_id is None
    assert event.replayed is False


def test_emit_debug_default_payload_is_empty_dict() -> None:
    emit_debug(
        category="system",
        severity="info",
        source="test.emit",
        summary="boot",
    )

    [event] = snapshot()
    assert event.payload == {}


def test_emit_debug_optional_ids_are_propagated() -> None:
    emit_debug(
        category="llm",
        severity="debug",
        source="test.emit",
        summary="LLM call",
        turn_id="turn-123",
        correlation_id="corr-456",
    )

    [event] = snapshot()
    assert event.turn_id == "turn-123"
    assert event.correlation_id == "corr-456"


def test_event_to_dict_matches_wire_envelope() -> None:
    emit_debug(
        category="llm",
        severity="info",
        source="bob.llm_client.complete",
        summary="LLM call démarré",
        payload={"messages": [{"role": "user", "content": "hi"}]},
        turn_id="t1",
        correlation_id="c1",
    )

    [event] = snapshot()
    wire = event.to_dict()
    # PRD `Schema sur le fil` field set.
    assert set(wire.keys()) == {
        "ts",
        "category",
        "severity",
        "source",
        "summary",
        "payload",
        "turn_id",
        "correlation_id",
        "replayed",
    }
    assert wire["turn_id"] == "t1"
    assert wire["correlation_id"] == "c1"
    assert wire["replayed"] is False


def test_timestamp_is_iso8601_with_z_suffix() -> None:
    emit_debug(
        category="input",
        severity="info",
        source="test.ts",
        summary="x",
    )

    [event] = snapshot()
    # Format: YYYY-MM-DDTHH:MM:SS.sssZ
    assert event.ts.endswith("Z")
    assert "T" in event.ts
    # Length sanity: 4+1+2+1+2+1+2+1+2+1+2+1+3+1 = 24
    assert len(event.ts) == 24


def test_ring_buffer_caps_at_maxlen() -> None:
    # The buffer caps at 2000; emit a few past the cap.
    for i in range(2005):
        emit_debug(
            category="input",
            severity="trace",
            source="test.cap",
            summary=f"event-{i}",
        )

    events = snapshot()
    assert len(events) == 2000
    # Oldest dropped — first remaining is event 5, last is 2004.
    assert events[0].summary == "event-5"
    assert events[-1].summary == "event-2004"


def test_no_subscribers_does_not_crash() -> None:
    """`emit_debug` is a pure side-effect when nobody listens."""

    assert subscriber_count() == 0
    emit_debug(
        category="system",
        severity="info",
        source="test.no_sub",
        summary="lonely",
    )
    # Buffer still grew.
    assert len(snapshot()) == 1


@pytest.mark.asyncio
async def test_subscribe_replays_snapshot_then_streams_live() -> None:
    # Seed two events before any subscriber connects.
    emit_debug(category="input", severity="info", source="t", summary="seed-1")
    emit_debug(category="input", severity="info", source="t", summary="seed-2")

    received: list[DebugEvent] = []

    async def consumer() -> None:
        async for event in subscribe():
            received.append(event)
            if len(received) >= 4:
                break

    task = asyncio.create_task(consumer())
    # Let the consumer drain the snapshot.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Now emit live; should land on the consumer's queue.
    emit_debug(category="input", severity="info", source="t", summary="live-1")
    emit_debug(category="input", severity="info", source="t", summary="live-2")

    await asyncio.wait_for(task, timeout=1.0)

    assert [e.summary for e in received] == ["seed-1", "seed-2", "live-1", "live-2"]
    # Snapshot events carry replayed=True; live events keep replayed=False.
    assert received[0].replayed is True
    assert received[1].replayed is True
    assert received[2].replayed is False
    assert received[3].replayed is False


@pytest.mark.asyncio
async def test_subscriber_unregisters_when_generator_closes() -> None:
    """Closing the subscribe generator releases the subscriber slot."""

    emit_debug(category="input", severity="info", source="t", summary="seed")

    gen = subscribe()
    first = await gen.__anext__()
    assert first.summary == "seed"
    assert subscriber_count() == 1

    # Explicit cleanup mimics what FastAPI's WS handler does on disconnect
    # (the consumer breaks out and the generator's finally runs).
    await gen.aclose()
    assert subscriber_count() == 0


@pytest.mark.asyncio
async def test_emit_is_non_blocking_when_subscriber_is_full() -> None:
    """A slow subscriber must not block the producer — overflow drops oldest."""

    queue: asyncio.Queue[DebugEvent] = asyncio.Queue(maxsize=3)
    debug_log._subscribers.append(queue)
    try:
        # Emit far more than the queue can hold. None of these should raise
        # or block — non-blocking contract.
        for i in range(50):
            emit_debug(
                category="input",
                severity="trace",
                source="t",
                summary=f"event-{i}",
            )

        # Queue must be at its cap, never above.
        assert queue.qsize() == 3
        # Drain and verify the LAST 3 events are what survived (drop_oldest).
        drained = [queue.get_nowait().summary for _ in range(3)]
        assert drained == ["event-47", "event-48", "event-49"]
    finally:
        debug_log._subscribers.remove(queue)


@pytest.mark.asyncio
async def test_two_concurrent_subscribers_get_independent_streams() -> None:
    received_a: list[str] = []
    received_b: list[str] = []

    async def consumer(target: list[str], n: int) -> None:
        async for event in subscribe():
            target.append(event.summary)
            if len(target) >= n:
                break

    task_a = asyncio.create_task(consumer(received_a, 2))
    task_b = asyncio.create_task(consumer(received_b, 2))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    emit_debug(category="input", severity="info", source="t", summary="a")
    emit_debug(category="input", severity="info", source="t", summary="b")

    await asyncio.wait_for(task_a, timeout=1.0)
    await asyncio.wait_for(task_b, timeout=1.0)

    assert received_a == ["a", "b"]
    assert received_b == ["a", "b"]
