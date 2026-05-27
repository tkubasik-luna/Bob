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

Slice 0039 — ContextVar propagation + structlog bridge
------------------------------------------------------

Slice 0038 left ``turn_id`` always ``None`` and asked callers to set it
explicitly. Slice 0039 wires a :class:`contextvars.ContextVar` named
``current_turn_id`` so every :func:`emit_debug` call inside a turn
automatically inherits the id, including any coroutine spawned via
:func:`asyncio.create_task` (standard ``contextvars`` semantics — a copy
of the calling context is snapshotted at spawn time).

The :func:`start_turn` helper generates a fresh UUID, sets it in the
ContextVar and returns it; the orchestrator calls this at the top of
``process_user_message``. Explicit ``turn_id=`` arguments to
:func:`emit_debug` still win — useful for tests and for code paths that
synthesise events outside any turn context.

The :func:`install_structlog_bridge` helper installs a
:class:`logging.Handler` on the ``bob`` root logger that auto-forwards
WARN / ERROR records to :func:`emit_debug` with ``category="system"``.
This is the safety net for failures that aren't explicitly instrumented.
Records flagged with ``_debug_emitted=True`` (or originating from
``bob.debug_log`` / ``bob.ws_debug`` themselves) are skipped to avoid
loops / duplication when a site explicitly emits before logging.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

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


# Slice 0039: ContextVar propagated automatically by ``contextvars`` to any
# coroutine spawned in the calling context. ``process_user_message`` sets it
# via :func:`start_turn`; ``emit_debug`` reads it when the caller doesn't
# pass ``turn_id=`` explicitly.
current_turn_id: ContextVar[str | None] = ContextVar("current_turn_id", default=None)

# Slice 0043: parallel ContextVar for the *currently-executing sub-task id*.
# Set via :func:`start_task` at the entry of ``SubAgentRunner.run`` so every
# :func:`emit_debug` triggered within the sub-task (LLM calls, progress / done
# / ask_user / fail handlers, anything spawned via :func:`asyncio.create_task`)
# inherits the id as ``parent_task_id``. When a sub-task itself spawns another
# sub-task, the nested ``start_task`` shadows the parent via reset-token so
# the inner sub-task's events carry the *immediate* parent. Default ``None``
# means "no enclosing sub-task" → ``parent_task_id`` on the event stays
# ``None`` (top-level orchestrator turn or pure system event).
current_task_id: ContextVar[str | None] = ContextVar("current_task_id", default=None)


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
      triggered by the same user turn. Slice 0039 auto-fills this from
      the ``current_turn_id`` ContextVar when the caller doesn't supply
      it explicitly.
    - ``correlation_id``: optional UUID linking ``*_start`` / ``*_end``
      pairs for long ops (LLM calls, sub-task runs).
    - ``parent_task_id``: optional id of the *enclosing sub-task* when the
      event was emitted from inside a :class:`SubAgentRunner.run` scope —
      slice 0043 auto-fills this from the ``current_task_id`` ContextVar.
      ``None`` for events emitted at the orchestrator level or outside any
      sub-task context. Kept for backward compat with the debug grouped
      tree (frontend ``groupEvents.ts``) which still keys on
      ``parent_task_id``.
    - ``task_id``: issue 0052 — the task this event belongs to. Auto-filled
      from the same ``current_task_id`` ContextVar as ``parent_task_id``
      (they alias the same value today; field exists so the per-task
      overlay subscription has a stable name to filter on regardless of
      whether the parent_task_id semantics ever diverge).
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
    parent_task_id: str | None = None
    task_id: str | None = None
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
            "parent_task_id": self.parent_task_id,
            "task_id": self.task_id,
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
            parent_task_id=self.parent_task_id,
            task_id=self.task_id,
            replayed=replayed,
        )


# Module-level state. The ring buffer outlives any individual WS connection
# and is the source of truth for the snapshot replay.
_buffer: deque[DebugEvent] = deque(maxlen=_RING_BUFFER_MAXLEN)
_subscribers: list[asyncio.Queue[DebugEvent]] = []

# Optional JSONL file sink (debugging aid). When installed via
# :func:`install_file_sink`, every emitted event is also appended as a single
# JSON line to a persistent file handle so the orchestration trace survives the
# process and can be read / grepped offline — the WS feed only lives as long as
# a client is connected and the ring buffer is bounded + in-memory. The handle
# is opened once and flushed per line so a crash still leaves a complete log.
_file_sink: TextIO | None = None
_file_sink_lock = threading.Lock()


def _now_iso() -> str:
    """Return the current UTC instant as an ISO 8601 string with ms precision.

    ``datetime.isoformat`` emits microseconds; we trim to milliseconds and
    use ``Z`` to mark UTC (the JS ``Date`` constructor parses both
    ``+00:00`` and ``Z`` but ``Z`` is more compact on the wire).
    """

    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def start_turn() -> str:
    """Generate a fresh ``turn_id`` and install it in the ContextVar.

    Called at the entry of :meth:`bob.orchestrator.Orchestrator.process_user_message`
    so every :func:`emit_debug` triggered during the turn — including those
    emitted by sub-tasks spawned via :func:`asyncio.create_task` from within
    the turn — inherits the same id. Returns the new id so the caller can
    log it / use it for explicit correlation if needed.

    The id is a hex UUID (32 chars, no dashes) to match the
    ``correlation_id`` shape used elsewhere.
    """

    turn_id = uuid.uuid4().hex
    current_turn_id.set(turn_id)
    return turn_id


def start_task(task_id: str) -> Token[str | None]:
    """Install ``task_id`` in the :data:`current_task_id` ContextVar.

    Symmetric to :func:`start_turn` but the caller already owns the id (the
    sub-task row was created by the orchestrator before scheduling). Returns
    the :class:`Token` produced by ``set`` so the caller can restore the
    previous value via ``current_task_id.reset(token)`` in a ``finally`` —
    this guarantees correct nesting when a sub-task spawns another sub-task
    inside the same execution context (e.g. tests).

    Slice 0043: :class:`SubAgentRunner.run` wraps its body in
    ``token = start_task(task_id); try: ... finally: current_task_id.reset(token)``
    so every :func:`emit_debug` inside the run inherits the id as
    ``parent_task_id``. Coroutines spawned via :func:`asyncio.create_task`
    snapshot the calling context (standard ``contextvars`` semantics) and
    inherit the value automatically.
    """

    return current_task_id.set(task_id)


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

    Slice 0039: when ``turn_id`` is not supplied explicitly, fall back to
    the ``current_turn_id`` ContextVar so callers inside a user turn
    don't have to thread the id manually.
    """

    effective_turn_id = turn_id if turn_id is not None else current_turn_id.get()
    # Slice 0043: ``parent_task_id`` is always read from the ContextVar —
    # there is no explicit override at the call site today (no producer
    # wants to forge a fake parent). Orphan events outside any
    # ``start_task`` scope keep the default ``None``.
    # Issue 0052: ``task_id`` aliases the same ContextVar so the per-task
    # overlay filter can key on a stable name. The two fields carry
    # identical values today; ``parent_task_id`` is preserved for the
    # debug grouped tree's existing grouping logic.
    effective_task_id = current_task_id.get()

    event = DebugEvent(
        ts=_now_iso(),
        category=category,
        severity=severity,
        source=source,
        summary=summary,
        payload=payload if payload is not None else {},
        turn_id=effective_turn_id,
        correlation_id=correlation_id,
        parent_task_id=effective_task_id,
        task_id=effective_task_id,
        replayed=False,
    )
    _buffer.append(event)
    # Issue 0052: enforce byte + age retention bounds. The deque maxlen
    # still protects against the worst-case pathological producer; the
    # policy adds dimensions the deque cannot express on its own.
    _enforce_retention()

    # Persist to the optional JSONL file sink before fanning out to live
    # subscribers. Unlike the ring buffer (bounded, in-memory) and the WS
    # subscribers (drop-oldest under backpressure), the file sink keeps a
    # complete, ordered trace on disk for offline debugging.
    _write_to_file_sink(event)

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


def snapshot_for_task(task_id: str) -> list[DebugEvent]:
    """Return the ring-buffer events tagged with ``task_id`` (issue 0052).

    Used by the ``/ws/task/{task_id}`` route to build the initial snapshot
    frame before tailing live events filtered by the same id. Matches
    against ``event.task_id`` (the issue 0052 field) — events emitted
    before the field existed simply do not match.
    """

    return [event for event in _buffer if event.task_id == task_id]


def clear() -> None:
    """Wipe the ring buffer. Intended for tests only.

    The active subscriber queues are NOT touched: a live consumer keeps
    seeing the events that were emitted during its lifetime.
    """

    _buffer.clear()


# --- JSONL file sink (offline debugging) -------------------------------------
#
# The ring buffer + WS subscribers are great for a live UI but useless once the
# process exits or when no client is connected: the buffer is in-memory and
# bounded, the subscriber queues drop-oldest under backpressure. The file sink
# closes that gap by appending every emitted event as a JSON line to a
# persistent handle, giving a complete, ordered, crash-durable trace that can
# be read / grepped offline. Installed from the FastAPI lifespan; opt-out via
# the ``ORCHESTRATION_LOG_ENABLED`` setting.


def install_file_sink(path: str | Path) -> None:
    """Open a JSONL file sink so every emitted event is also written to disk.

    Idempotent: a second call while a sink is already open keeps the existing
    handle. The parent directory is created if missing; the file is opened in
    append mode and accumulates across process restarts. A ``session_start``
    marker line is written directly to the file (not via :func:`emit_debug`)
    so each run is delimited on disk without polluting the live WS feed.
    """

    global _file_sink
    with _file_sink_lock:
        if _file_sink is not None:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _file_sink = target.open("a", encoding="utf-8")
        marker = {
            "ts": _now_iso(),
            "category": "system",
            "severity": "info",
            "source": "debug_log.install_file_sink",
            "summary": "=== session start ===",
            "payload": {"path": str(target), "pid": os.getpid()},
            "turn_id": None,
            "correlation_id": None,
            "parent_task_id": None,
            "task_id": None,
            "replayed": False,
        }
        with contextlib.suppress(Exception):
            _file_sink.write(json.dumps(marker, ensure_ascii=False) + "\n")
            _file_sink.flush()


def uninstall_file_sink() -> None:
    """Flush + close the JSONL file sink. Idempotent."""

    global _file_sink
    with _file_sink_lock:
        if _file_sink is None:
            return
        with contextlib.suppress(Exception):
            _file_sink.flush()
            _file_sink.close()
        _file_sink = None


def _write_to_file_sink(event: DebugEvent) -> None:
    """Append ``event`` to the JSONL file sink if one is installed.

    Fire-and-forget: any serialisation / I/O error is swallowed so a broken
    sink never propagates into the producer hot path. ``default=str`` guards
    payloads carrying non-JSON-native values (datetimes, enums, ...). Flushed
    per line so a crash still leaves a complete log.
    """

    if _file_sink is None:
        return
    try:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
    except Exception:
        return
    with _file_sink_lock:
        sink = _file_sink
        if sink is None:
            return
        try:
            sink.write(line + "\n")
            sink.flush()
        except Exception:
            return


# --- Retention policy hook (issue 0052) --------------------------------------
#
# The ring buffer is bounded by ``_RING_BUFFER_MAXLEN`` already — that's a
# count cap. Issue 0052 adds two extra dimensions enforced on every emit:
#
# - ``max_bytes``: total estimated bytes of the buffered events. Estimated
#   via :func:`len(json.dumps(event.to_dict()))` so the size accounts for
#   wire-shape (which is what an overlay client actually streams).
# - ``max_age_seconds``: events older than this are dropped before the
#   buffer is consulted. Wall-clock based; we re-parse the ``ts`` field
#   via :func:`datetime.fromisoformat` (the ``Z`` suffix is normalised to
#   ``+00:00`` first).
#
# The policy module owns the configuration object so tests can install a
# tight policy and assert eviction. We never enforce a count cap here —
# the deque ``maxlen`` already does that.


# Imported lazily inside the hook to avoid a top-level cycle: the policy
# module exports a function but the policy itself is a tiny dataclass that
# imports nothing from debug_log.
def _enforce_retention() -> None:
    from bob.event_retention_policy import get_retention_policy

    policy = get_retention_policy()
    if policy is None:
        return

    # Age-based eviction first — cheaper than measuring bytes.
    now = datetime.now(UTC)
    if policy.max_age_seconds is not None:
        threshold = policy.max_age_seconds
        # popleft until the oldest event is within the threshold. We do
        # not iterate to avoid mutating the deque during iteration.
        while _buffer:
            oldest = _buffer[0]
            try:
                ts = oldest.ts.replace("Z", "+00:00")
                age = (now - datetime.fromisoformat(ts)).total_seconds()
            except ValueError:
                # Malformed timestamp — drop, can't reason about its age.
                _buffer.popleft()
                continue
            if age <= threshold:
                break
            _buffer.popleft()

    # Bytes-based eviction. Recompute total each loop pass; the deque is
    # bounded by ``_RING_BUFFER_MAXLEN`` so the iteration is short.
    if policy.max_bytes is not None:

        def total_bytes() -> int:
            import json

            return sum(len(json.dumps(event.to_dict())) for event in _buffer)

        while _buffer and total_bytes() > policy.max_bytes:
            _buffer.popleft()


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


async def subscribe_filtered(
    predicate: Callable[[DebugEvent], bool],
) -> AsyncGenerator[DebugEvent, None]:
    """Async-generator over the producer with a per-event filter (issue 0052).

    Behaves like :func:`subscribe` but only yields events for which
    ``predicate(event)`` returns ``True``. The snapshot pass and the live
    tail share the same predicate so the consumer sees a single coherent
    stream — no need to wire a filter twice.

    The producer side is untouched: every emitted event still lands on
    every subscriber queue (the broadcast cost is per-event, not per-id),
    we just drop non-matching events at yield time. This is the right
    trade-off for the small number of concurrent overlay subscribers Bob
    will see (single-user desktop app) and keeps the producer fully
    independent of subscriber filter shapes.
    """

    queue: asyncio.Queue[DebugEvent] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
    _subscribers.append(queue)
    try:
        for event in snapshot():
            if predicate(event):
                yield event.with_replayed(True)

        while True:
            event = await queue.get()
            if predicate(event):
                yield event
    finally:
        with contextlib.suppress(ValueError):
            _subscribers.remove(queue)


def subscriber_count() -> int:
    """Return the number of active subscribers. Test helper."""

    return len(_subscribers)


# ---------------------------------------------------------------------------
# Structlog bridge (slice 0039)
#
# Safety net: forward any WARN/ERROR log record emitted on a ``bob.*`` logger
# to :func:`emit_debug` as a ``system`` event, so failures that aren't
# explicitly instrumented still surface in the debug feed. The bridge skips
# records that are themselves emitted from inside ``debug_log`` / ``ws_debug``
# to avoid feedback loops, and respects an explicit ``_debug_emitted=True``
# marker for sites that already wrote their own ``emit_debug`` and don't want
# the bridge to duplicate.
# ---------------------------------------------------------------------------


# Loggers whose records the bridge ignores entirely. The bridge itself logs
# nothing today, but any future :mod:`logging` call from these modules MUST
# NOT loop back into ``emit_debug``.
_BRIDGE_SOURCE_BLACKLIST = frozenset(
    {
        "bob.debug_log",
        "bob.ws_debug",
    }
)

# Standard ``LogRecord`` attribute names. Anything outside this set on a
# record's ``__dict__`` is treated as a user-supplied structured field and
# pulled into the event payload.
_STD_LOGRECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class _DebugBridgeHandler(logging.Handler):
    """Forward WARN+ records on ``bob.*`` loggers to :func:`emit_debug`.

    Idempotent install: :func:`install_structlog_bridge` only attaches a
    single instance to the ``bob`` root logger, so re-running the lifespan
    (tests, dev reloads) doesn't multiply forwarded events.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        # Defensive: never let a misbehaving record kill the producer side
        # of the logging pipeline. The handler is a safety net, not a hard
        # contract — if we can't forward, log nothing and move on.
        try:
            self._forward(record)
        except Exception:
            # Last-ditch: drop the record. We intentionally don't call
            # ``self.handleError(record)`` because the default impl writes
            # to stderr which Bob already routes through structlog — it
            # would muddle the JSON output.
            return

    @staticmethod
    def _forward(record: logging.LogRecord) -> None:
        # Skip records emitted from inside the bridge / debug WS layer to
        # avoid feedback loops. ``record.name`` is the logger name (e.g.
        # ``bob.debug_log``) — we match dotted-prefix style.
        if record.name in _BRIDGE_SOURCE_BLACKLIST:
            return

        # Skip records flagged as already-emitted via an explicit
        # ``emit_debug`` at the call site. Sites set this by passing
        # ``extra={"_debug_emitted": True}`` to the logging call.
        if getattr(record, "_debug_emitted", False):
            return

        severity: DebugSeverity = "error" if record.levelno >= logging.ERROR else "warn"

        # Pull user-supplied structured fields (anything not a standard
        # ``LogRecord`` attribute) into the payload so structured logger
        # call sites (``_logger.warning("foo", task_id="x")``) keep their
        # context in the debug view.
        payload: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _STD_LOGRECORD_ATTRS:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = logging.Formatter().formatException(record.exc_info)

        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)

        emit_debug(
            category="system",
            severity=severity,
            source=record.name,
            summary=message,
            payload=payload,
        )


_bridge_handler: _DebugBridgeHandler | None = None


def install_structlog_bridge() -> None:
    """Attach the :class:`_DebugBridgeHandler` to the ``bob`` root logger.

    Idempotent: a second call detects the existing handler and is a no-op.
    Called from the FastAPI lifespan once the logging configuration is in
    place. Tests can call it explicitly when they need the bridge active
    (most tests don't — the bridge is a safety net for live operation).
    """

    global _bridge_handler
    if _bridge_handler is not None:
        return
    handler = _DebugBridgeHandler()
    logging.getLogger("bob").addHandler(handler)
    _bridge_handler = handler


def uninstall_structlog_bridge() -> None:
    """Remove the bridge handler from the ``bob`` root logger.

    Idempotent. Mainly useful for tests that want to reset module-level
    state between cases.
    """

    global _bridge_handler
    if _bridge_handler is None:
        return
    logging.getLogger("bob").removeHandler(_bridge_handler)
    _bridge_handler = None
