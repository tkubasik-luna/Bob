"""In-process debug event log — backend half of PRD 0005 (debug view).

This module is the deep / pure layer of the debug pipeline. It has no
FastAPI dependency so it can be unit-tested in isolation and so the
``emit_debug`` helper can be sprinkled across modules that themselves
know nothing about the WS transport.

Pipeline shape
--------------

1. Producers call :func:`emit_debug` with a category / severity / source
   tuple plus a human-written ``summary``. The helper builds a frozen
   :class:`DebugEvent`, appends it to a bounded :class:`collections.deque`
   (the *ring buffer*) and pushes it to every active subscriber's queue.
2. Consumers — typically :mod:`bob.ws_debug` — call :func:`subscribe`
   to obtain an async generator that yields first the snapshot of the
   ring buffer (events tagged ``replayed=True``) and then streams new
   events as they arrive.

Non-blocking contract
---------------------

The producer side MUST never block on a slow / disconnected consumer.
Each subscriber owns an :class:`asyncio.Queue` with a bounded capacity;
when the queue is full :func:`emit_debug` drops the *oldest* queued
event for that subscriber (the new event is still delivered) so the
orchestrator and LLM call paths stay snappy regardless of how many
debug clients are connected. With zero subscribers the ring buffer
simply continues to accumulate.

Slice 0038 scope
----------------

The PRD describes a richer pipeline (``contextvars`` propagation of
``turn_id``, structlog bridge for WARN/ERROR logs). This module ships
only the envelope, the ring buffer, the non-blocking emit pipeline and
the subscribe iterator. ``turn_id`` is passed explicitly by the caller
or left ``None``; the structlog bridge arrives in slice 0039.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

DebugCategory = Literal[
    "input",
    "llm",
    "decision",
    "task",
    "output",
    "voice",
    "system",
]

DebugSeverity = Literal["trace", "debug", "info", "warn", "error"]


_RING_BUFFER_MAXLEN = 2000
_SUBSCRIBER_QUEUE_MAXSIZE = 500


@dataclass(frozen=True)
class DebugEvent:
    """A single debug event, immutable once emitted.

    Field meanings track the PRD envelope:

    - ``ts``: ISO 8601 UTC timestamp with millisecond precision, e.g.
      ``2026-05-25T14:23:01.123Z``. Generated automatically by
      :func:`emit_debug`.
    - ``category``: one of seven coarse buckets the UI filters on.
    - ``severity``: log-level-like ordering, ``trace`` lowest.
    - ``source``: dotted path identifying the emit site, free-form
      string (e.g. ``orchestrator.process_user_message``).
    - ``summary``: one-line human-readable description rendered in the
      feed as the primary text.
    - ``payload``: free-form dict carrying the raw detail (LLM messages,
      exception trace, ...). Defaults to empty dict.
    - ``turn_id``: optional UUID-like string grouping every event
      triggered by the same user turn. ``None`` until slice 0039 wires
      the ContextVar propagation.
    - ``correlation_id``: optional UUID linking ``*_start`` / ``*_end``
      pairs for long ops (LLM calls, sub-task runs).
    - ``replayed``: ``True`` when the event was yielded from the ring
      buffer snapshot at subscribe time, ``False`` for live emits.
    """

    ts: str
    category: DebugCategory
    severity: DebugSeverity
    source: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    correlation_id: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Wire shape for the WS layer — matches the PRD `Schema sur le fil`."""

        return {
            "ts": self.ts,
            "category": self.category,
            "severity": self.severity,
            "source": self.source,
            "summary": self.summary,
            "payload": self.payload,
            "turn_id": self.turn_id,
            "correlation_id": self.correlation_id,
            "replayed": self.replayed,
        }

    def with_replayed(self, replayed: bool) -> DebugEvent:
        """Return a copy with the ``replayed`` flag flipped.

        The subscribe iterator uses this to tag the snapshot pass without
        mutating the canonical ring-buffer entries.
        """

        return DebugEvent(
            ts=self.ts,
            category=self.category,
            severity=self.severity,
            source=self.source,
            summary=self.summary,
            payload=self.payload,
            turn_id=self.turn_id,
            correlation_id=self.correlation_id,
            replayed=replayed,
        )


# Module-level state. The ring buffer outlives any individual WS connection
# and is the source of truth for the snapshot replay.
_buffer: deque[DebugEvent] = deque(maxlen=_RING_BUFFER_MAXLEN)
_subscribers: list[asyncio.Queue[DebugEvent]] = []


def _now_iso() -> str:
    """Return the current UTC instant as an ISO 8601 string with ms precision.

    ``datetime.isoformat`` emits microseconds; we trim to milliseconds and
    use ``Z`` to mark UTC (the JS ``Date`` constructor parses both
    ``+00:00`` and ``Z`` but ``Z`` is more compact on the wire).
    """

    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def emit_debug(
    *,
    category: DebugCategory,
    severity: DebugSeverity,
    source: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    turn_id: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Build a :class:`DebugEvent` and broadcast it to every subscriber.

    Always pushes the event to the ring buffer first so a late-arriving
    subscriber sees the history. Subscriber queues are then fed with a
    bounded ``put_nowait`` — if a queue is saturated the oldest queued
    event for that subscriber is dropped to make room.

    Producers MUST NOT await on the result; the call is synchronous and
    fire-and-forget. If no event loop is running (e.g. a sync test
    importing the producer code path), the ring buffer is still updated
    so :func:`snapshot` reflects the call.
    """

    event = DebugEvent(
        ts=_now_iso(),
        category=category,
        severity=severity,
        source=source,
        summary=summary,
        payload=payload if payload is not None else {},
        turn_id=turn_id,
        correlation_id=correlation_id,
        replayed=False,
    )
    _buffer.append(event)

    # Drop strategy: drop the oldest queued event for the slow subscriber,
    # then place the new one. We iterate over a snapshot of the subscriber
    # list so subscribe / unsubscribe during emit don't blow up.
    for queue in list(_subscribers):
        while True:
            try:
                queue.put_nowait(event)
                break
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Race: queue drained between full check and get.
                    # Loop back, the next put_nowait will succeed.
                    continue


def snapshot() -> list[DebugEvent]:
    """Return a list copy of the ring buffer in insertion order.

    Each event is returned with ``replayed=False`` — callers wanting to
    tag historical events use :meth:`DebugEvent.with_replayed`.
    """

    return list(_buffer)


def clear() -> None:
    """Wipe the ring buffer. Intended for tests only.

    The active subscriber queues are NOT touched: a live consumer keeps
    seeing the events that were emitted during its lifetime.
    """

    _buffer.clear()


async def subscribe() -> AsyncGenerator[DebugEvent, None]:
    """Async-generator: replay the snapshot, then stream live events forever.

    The generator yields:

    1. Every event currently in the ring buffer, tagged with
       ``replayed=True`` so the UI can distinguish history from live.
    2. Live events as :func:`emit_debug` calls land on the subscriber's
       queue, with ``replayed=False`` (their canonical flag).

    The generator runs until the consumer breaks out of the iteration
    (FastAPI's WebSocket handler does this on disconnect via
    ``WebSocketDisconnect``); the ``finally`` clause unregisters the
    subscriber so we don't leak queues.
    """

    queue: asyncio.Queue[DebugEvent] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
    _subscribers.append(queue)
    try:
        # 1) snapshot replay. Events inserted during the snapshot copy
        # were already buffered; live events emitted *after* this point
        # land on our queue. There can be a tiny duplicate window if an
        # emit happens between the buffer copy and the queue draining,
        # but the frontend slice 0038 simply renders the duplicate (one
        # extra line); deduping is a future-slice concern.
        for event in snapshot():
            yield event.with_replayed(True)

        # 2) live tail.
        while True:
            event = await queue.get()
            yield event
    finally:
        # ``ValueError`` here means a concurrent path already removed the
        # subscriber — swallowing it is safe; the queue is dropped anyway.
        with contextlib.suppress(ValueError):
            _subscribers.remove(queue)


def subscriber_count() -> int:
    """Return the number of active subscribers. Test helper."""

    return len(_subscribers)
