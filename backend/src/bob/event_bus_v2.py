"""Unified event producer for telemetry + per-task overlay (issue 0052).

Why a new module rather than migrating :mod:`bob.event_bus` in place?
:mod:`bob.event_bus` already exports an :class:`EventBus` class — but that
one is an *in-process pub/sub* used by the sub-agent runner and the
proactivity handler to coordinate on topics like ``task_state_changed``
and ``task_message_added``. It is NOT the debug-event ring buffer.

This module unifies two previously parallel producers into a single one:

- :func:`bob.debug_log.emit_debug` — the structured debug event ring
  buffer (PRD 0005). Already the only path for the debug feed.
- :func:`bob.ws_events.emit` — the WS broadcaster that pushes task
  lifecycle events (``task_created``, ``task_updated``, ``task_result``,
  ``task_message``, ``assistant_msg``) to the connected chat client.

Pre-0052 these two channels were strictly parallel: a sub-agent runner
would call BOTH ``ws_events.emit`` (to update the sidebar) AND
``emit_debug`` (to populate the debug feed). The user-facing surface
remained unchanged; the wire-shape duplication was paid every time.

Issue 0052 collapses them by:

1. Routing every ``ws_events.emit`` payload through :func:`emit_event`
   here, which both:
   - appends a :class:`DebugEvent` to the ring buffer (so the debug feed
     keeps seeing the same events), AND
   - forwards the original wire payload to the registered WS emitter
     (so the chat client keeps receiving its task events).
2. Adding a filtered subscription :func:`subscribe_for_task` so the
   ``/ws/task/{task_id}`` overlay can tap the ring buffer with no new
   storage layer. Snapshot + tail are served from the same WS session
   (see :mod:`bob.ws_router`).

The producer side is a thin wrapper — there is only ONE underlying ring
buffer (:mod:`bob.debug_log` owns it) so there cannot be drift between
the debug feed and the per-task overlay. That's the whole point of the
unification.

Reflections (sub-agent ``thought`` / ``tool_invoke`` / ``tool_result`` /
``addendum_received`` / ``status_change``) flow through ``emit_debug``
already (see :mod:`bob.sub_agent.runner`). They live ONLY in the ring
buffer — never in SQLite. Process restart loses them, by design: the
sub-agent runs themselves don't survive a restart, so persisting their
inner reasoning would be lying about state continuity. ``task_completed``
history survives via the existing ``tasks`` / ``task_messages`` SQLite
tables.

Concurrency contract: every public function in this module is safe to
call without holding any lock. The producer side uses :func:`emit_debug`
which already handles the bounded-queue / drop-oldest contract for slow
subscribers. The forwarder side wraps the legacy WS emitter, which is
``async`` and may itself block on a slow socket — every per-emitter
forward is therefore bounded by ``WS_EMITTER_TIMEOUT_SECONDS`` and an
emitter that times out or raises is evicted from the registry on first
failure (issue 0122), so a zombie window can never freeze the
orchestrator.

Hot event batching (PRD 0018 / issue 0123)
------------------------------------------

The token-by-token streams — ``speech_delta`` (one frame per parser tick)
and ``reasoning_delta`` (one frame per reasoning token) — used to pay the
full pipeline (ring-buffer append + retention sweep + JSONL line + one WS
frame per window) for EVERY token. :func:`emit_event` now coalesces them:
a hot event is buffered per ``(type, key)`` — ``msg_id`` for speech,
``agent_ref`` for reasoning — and at most one MERGED event per key is
emitted per ``WS_HOT_EVENT_BATCH_WINDOW_MS`` window. The merged event
keeps the exact same wire type and shape with the ``delta`` fields
concatenated, so the frontend consumers (which already accumulate deltas
per key) need no change. Everything else stays immediate; a cold event
flushes any pending window FIRST, so the relative order "deltas precede
their closing ``ui_payload`` / ``assistant_msg``" is preserved. Setting
the window to ``0`` disables coalescing entirely.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from bob.config import get_settings
from bob.debug_log import (
    DebugCategory,
    DebugEvent,
    DebugSeverity,
    current_task_id,
    emit_debug,
    replace_last_event,
    snapshot,
    snapshot_for_task,
    subscribe_filtered,
)

_logger = structlog.get_logger(__name__)


#: Wire shape for the ``/ws/chat`` channel — opaque dict, identical to
#: what the legacy :mod:`bob.ws_events` accepted. Kept loose because the
#: payload set spans ``task_created``, ``task_updated``, ``task_result``,
#: ``task_message``, ``assistant_msg``, ``speech_delta`` (0049), ...
WsTaskEvent = dict[str, Any]


#: Async forwarders for the chat WS frame — ONE per connected window/session.
#: Each ``/ws/chat`` session registers its emitter via :func:`add_ws_emitter`
#: for its lifetime and unregisters on disconnect.
#:
#: This used to be a single slot (last-writer-wins). With more than one window
#: open (sphere HUD + legacy chat + debug), each opens its own ``/ws/chat``, so
#: the last connection silently stole the channel: task lifecycle events,
#: streamed ``speech_delta`` / ``ui_payload`` frames and proactive pushes only
#: reached that one window — the others (including the window that asked the
#: question) got nothing. That is why a Markdown overlay / proactive TTS could
#: land on the wrong window or vanish. :func:`emit_event` now fans out to every
#: registered session.
WsEmitter = Callable[[WsTaskEvent], Awaitable[None]]
_ws_emitters: set[WsEmitter] = set()


def add_ws_emitter(fn: WsEmitter) -> None:
    """Register a connected session's chat WS forwarder (fan-out target)."""

    _ws_emitters.add(fn)


def remove_ws_emitter(fn: WsEmitter) -> None:
    """Unregister a session's forwarder on disconnect. Idempotent."""

    _ws_emitters.discard(fn)


def set_ws_emitter(fn: WsEmitter | None) -> None:
    """Replace the whole emitter set with a single ``fn`` (or clear on ``None``).

    Back-compat for single-emitter call sites + tests. The live WS layer uses
    :func:`add_ws_emitter` / :func:`remove_ws_emitter` so multiple windows each
    receive the fan-out; this helper keeps the one-emitter test ergonomics.
    """

    _ws_emitters.clear()
    if fn is not None:
        _ws_emitters.add(fn)


def snapshot_ws_emitters() -> set[WsEmitter]:
    """Return a copy of the currently-registered emitter set.

    Lets a harness (issue 0099 attest drive layer) temporarily take over the
    channel and restore the prior emitters afterwards via
    :func:`add_ws_emitter`, without poking the module-private set directly.
    """

    return set(_ws_emitters)


# --- Hot event batching (PRD 0018 / issue 0123) -------------------------------
#
# High-frequency delta streams are coalesced per ``(type, key)`` over a short
# window before they hit the (single, shared) emit pipeline. The merged event
# keeps the same wire type with the ``delta`` fields concatenated — the
# frontend consumers already accumulate deltas per key, so the wire contract
# is unchanged, only the frame cadence drops from per-token to per-window.

#: Hot wire types → the payload field that scopes one logical stream. Two
#: streams (e.g. Jarvis + a sub-agent both reasoning) never merge into one
#: frame because the key is part of the buffer slot.
_HOT_EVENT_KEY_FIELDS: dict[str, str] = {
    "speech_delta": "msg_id",
    "reasoning_delta": "agent_ref",
}


@dataclass
class _PendingHotEvent:
    """One buffered (merging) hot stream awaiting its window flush."""

    payload: WsTaskEvent
    category: DebugCategory
    severity: DebugSeverity
    source: str
    summary: str | None
    #: Context snapshot of the FIRST buffered delta — the flush may run from
    #: the timer task or a cold emit in a foreign context, and the ring-buffer
    #: side must still inherit the producer's ``current_turn_id`` /
    #: ``current_task_id`` ContextVars.
    context: contextvars.Context = field(default_factory=contextvars.copy_context)


class _HotEventBatcher:
    """Per-(type, key) coalescing buffer with a trailing window timer.

    Buffering is synchronous (no await — the producer returns immediately);
    the flush is driven by a single timer task that fires once per window
    while anything is pending, or eagerly by :func:`emit_event` when a cold
    event must not overtake the buffered deltas. Memory stays bounded: one
    slot per live ``(type, key)`` stream, each holding one growing string for
    at most one window.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], _PendingHotEvent] = {}
        self._timer: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._flushing = False

    def ensure_loop(self) -> None:
        """Bind (or re-bind) the batcher to the running event loop.

        The module-level instance outlives test event loops; state buffered
        on a dead loop (its lock, its timer) cannot be reused, so a loop
        change drops everything and starts clean. In production there is one
        loop for the process lifetime and this is a pointer comparison.
        """

        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._pending.clear()
        self._timer = None
        self._lock = asyncio.Lock()
        self._flushing = False

    def has_work(self) -> bool:
        """True when buffered deltas exist or a flush is in flight."""

        return bool(self._pending) or self._flushing or self._lock.locked()

    def buffer(
        self,
        event_type: str,
        key: str,
        payload: WsTaskEvent,
        *,
        category: DebugCategory,
        severity: DebugSeverity,
        source: str,
        summary: str | None,
        window_seconds: float,
    ) -> None:
        """Merge ``payload`` into its stream slot; arm the window timer."""

        slot = (event_type, key)
        pending = self._pending.get(slot)
        if pending is None:
            self._pending[slot] = _PendingHotEvent(
                payload=dict(payload),
                category=category,
                severity=severity,
                source=source,
                summary=summary,
            )
        else:
            pending.payload["delta"] = str(pending.payload.get("delta", "")) + str(
                payload.get("delta", "")
            )
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(
                self._run_timer(window_seconds), name="event_bus_v2.hot_event_flush"
            )

    async def flush(self) -> None:
        """Drain every pending stream now (cold-event ordering + teardown)."""

        async with self._lock:
            await self._flush_locked()
        self._cancel_timer_if_idle()

    async def _run_timer(self, window_seconds: float) -> None:
        """Fire one flush per window while anything is pending, then retire."""

        try:
            while True:
                await asyncio.sleep(window_seconds)
                async with self._lock:
                    await self._flush_locked()
                if not self._pending:
                    return
        except asyncio.CancelledError:
            # An eager flush already drained us (or the loop is going down).
            return

    async def _flush_locked(self) -> None:
        """Emit every buffered merged event, in first-arrival order."""

        while self._pending:
            entries = list(self._pending.values())
            self._pending.clear()
            self._flushing = True
            try:
                for entry in entries:
                    await _emit_event_now(
                        entry.payload,
                        category=entry.category,
                        severity=entry.severity,
                        source=entry.source,
                        summary=entry.summary,
                        debug_payload=None,
                        context=entry.context,
                    )
            finally:
                self._flushing = False

    def _cancel_timer_if_idle(self) -> None:
        """Retire the window timer once nothing is pending (no task leak)."""

        timer = self._timer
        if timer is None or timer.done() or timer is asyncio.current_task():
            return
        if not self._pending:
            timer.cancel()
            self._timer = None


_hot_batcher = _HotEventBatcher()


async def flush_hot_events() -> None:
    """Force-emit any buffered hot deltas now. Test / teardown hook."""

    _hot_batcher.ensure_loop()
    await _hot_batcher.flush()


async def emit_event(
    payload: WsTaskEvent,
    *,
    category: DebugCategory = "task",
    severity: DebugSeverity = "info",
    source: str = "bob.event_bus_v2.emit_event",
    summary: str | None = None,
    debug_payload: WsTaskEvent | None = None,
) -> None:
    """Single producer for WS task events — also lands in the ring buffer.

    Every legacy ``ws_events.emit(payload)`` call now routes through here.
    The call:

    - Appends a :class:`DebugEvent` carrying ``payload`` so the debug feed
      AND the per-task overlay subscription see the event. ``task_id`` is
      pulled from the ContextVar (sub-agent code paths) OR from
      ``payload["task_id"]`` when emitted at the orchestrator / scheduler
      level (no enclosing sub-task context). The latter is important for
      overlay filtering: a ``task_created`` emitted by the orchestrator
      MUST still surface on the per-task subscription.
    - Forwards the same payload to the registered WS emitter so the chat
      client's existing handlers (sidebar, task drawer) keep working.

    Privacy (PRD 0008 / issue 0064 building on 0056): the chat WS frame may
    carry sensitive fields the debug sinks must NOT see — e.g. a
    ``task_result`` event now ships the REAL Mail ``result_payload`` (subject /
    bodyPreview) so the overlay can render, but the debug ring buffer +
    ``/ws/debug`` feed + JSONL sink may only hold redacted metadata. Pass
    ``debug_payload`` with a scrubbed copy: it is what lands in the ring
    buffer, while the unmodified ``payload`` is still forwarded to the chat
    WS. When ``None`` (the default) the same ``payload`` is used for both, so
    every existing call site is unchanged.

    Concurrency: the debug emit is synchronous; the WS forwards are awaited
    (concurrently, one task per emitter) so the caller observes completion —
    but each forward is capped at ``WS_EMITTER_TIMEOUT_SECONDS`` and a
    timed-out / raising emitter is evicted from the registry (issue 0122),
    so back-pressure from a dead socket is bounded instead of unbounded.

    Hot batching (PRD 0018 / issue 0123): a ``speech_delta`` /
    ``reasoning_delta`` payload does not emit immediately — it merges into
    its per-key buffer and the whole pipeline (ring buffer + JSONL + WS
    fan-out) runs at most once per ``WS_HOT_EVENT_BATCH_WINDOW_MS`` window
    per stream, on a merged event of the same wire shape. Every other event
    type flushes the pending buffers FIRST and then emits with zero added
    delay, so cross-type ordering on the wire matches the call order.
    """

    _hot_batcher.ensure_loop()

    event_type = payload.get("type")
    if isinstance(event_type, str) and event_type in _HOT_EVENT_KEY_FIELDS:
        key = payload.get(_HOT_EVENT_KEY_FIELDS[event_type])
        window_ms = get_settings().WS_HOT_EVENT_BATCH_WINDOW_MS
        if isinstance(key, str) and key and window_ms > 0 and debug_payload is None:
            _hot_batcher.buffer(
                event_type,
                key,
                payload,
                category=category,
                severity=severity,
                source=source,
                summary=summary,
                window_seconds=window_ms / 1000.0,
            )
            return

    # Cold path: never delayed — but any buffered hot deltas must reach the
    # wire first, or a closing ``assistant_msg`` / ``ui_payload`` would
    # overtake the deltas it concludes.
    if _hot_batcher.has_work():
        await _hot_batcher.flush()
    await _emit_event_now(
        payload,
        category=category,
        severity=severity,
        source=source,
        summary=summary,
        debug_payload=debug_payload,
        context=None,
    )


async def _emit_event_now(
    payload: WsTaskEvent,
    *,
    category: DebugCategory,
    severity: DebugSeverity,
    source: str,
    summary: str | None,
    debug_payload: WsTaskEvent | None,
    context: contextvars.Context | None,
) -> None:
    """The actual emit pipeline: ring buffer append + WS fan-out.

    ``context`` is non-``None`` only for merged hot events flushed outside
    their producer's task (the window timer, or a sibling producer's cold
    emit): the ring-buffer side runs inside that snapshot so the event still
    inherits the producer's ``current_turn_id`` / ``current_task_id``.
    """

    captured_payload = debug_payload if debug_payload is not None else payload
    if context is not None:
        context.run(
            _emit_to_ring_buffer, payload, captured_payload, category, severity, source, summary
        )
    else:
        _emit_to_ring_buffer(payload, captured_payload, category, severity, source, summary)

    emitters = list(_ws_emitters)
    if not emitters:
        return
    # Snapshot the set: a forwarder may disconnect (and remove itself) while we
    # await a slow sibling. Forwards run concurrently and each one is bounded
    # by ``WS_EMITTER_TIMEOUT_SECONDS``; a hung or crashing socket is evicted
    # from the registry on first failure (issue 0122) so it receives nothing
    # further and can never block the other windows or the producer.
    timeout = get_settings().WS_EMITTER_TIMEOUT_SECONDS
    if len(emitters) == 1:
        # Fast path: a direct await adds no extra task hop, so a
        # single-window session keeps the exact pre-0122 loop-turn cadence
        # (producers and tests that yield a fixed number of turns after an
        # emit keep observing the event on time).
        await _forward_to_emitter(emitters[0], payload, timeout)
        return
    await asyncio.gather(*(_forward_to_emitter(emitter, payload, timeout) for emitter in emitters))


def _emit_to_ring_buffer(
    payload: WsTaskEvent,
    captured_payload: WsTaskEvent,
    category: DebugCategory,
    severity: DebugSeverity,
    source: str,
    summary: str | None,
) -> None:
    """Synchronous debug-side half of the emit: ring buffer + task-id patch."""

    # Resolve the task id: prefer the explicit ``payload["task_id"]``
    # (orchestrator/scheduler level) over the ContextVar (sub-agent
    # level). When neither is set, the event has no task association
    # and is invisible to per-task overlay subscriptions.
    payload_task_id = payload.get("task_id")
    resolved_task_id: str | None
    if isinstance(payload_task_id, str) and payload_task_id:
        resolved_task_id = payload_task_id
    else:
        resolved_task_id = current_task_id.get()

    effective_summary = summary or _default_summary(captured_payload)
    emit_debug(
        category=category,
        severity=severity,
        source=source,
        summary=effective_summary,
        payload={"ws_event": captured_payload},
        # When the producer is the orchestrator/scheduler (no ContextVar
        # task), we still want the per-task overlay to pick up this
        # event. We forge the field via the explicit ``task_id`` route on
        # :func:`emit_debug` — see the inline implementation below.
        # Implementation note: ``emit_debug`` reads ``current_task_id``
        # unconditionally; we can't override that today without breaking
        # the existing ContextVar contract. Instead, we patch the
        # produced event's ``task_id`` in the ring buffer post-hoc when
        # the ContextVar is empty but the payload carries one. This is
        # safe because :class:`DebugEvent` is frozen — we replace the
        # last buffer entry with a copy carrying the resolved id.
    )
    if resolved_task_id is not None and current_task_id.get() is None:
        # Patch the just-appended event so it carries the payload-derived
        # task id (the ContextVar produced ``None`` for it).
        _patch_last_event_task_id(resolved_task_id)


async def _forward_to_emitter(emitter: WsEmitter, payload: WsTaskEvent, timeout: float) -> None:
    """Forward ``payload`` to one emitter, bounded by ``timeout``. Evict on failure.

    Eviction contract (issue 0122): an emitter that times out or raises is
    removed from the registry immediately — it receives nothing further and
    can no longer block the fan-out, and the set keeps no dead references.
    The WS layer registers a fresh forwarder when the window reconnects, so
    eviction is never fatal to a healthy client. Never raises: a failing
    emitter must not poison its siblings inside the same :func:`asyncio.gather`.
    """

    try:
        await asyncio.wait_for(emitter(payload), timeout=timeout)
    except TimeoutError:
        remove_ws_emitter(emitter)
        _logger.warning(
            "event_bus_v2.ws_emitter_evicted",
            reason="timeout",
            timeout_seconds=timeout,
            emitter=_emitter_name(emitter),
            event_type=payload.get("type"),
        )
    except Exception:
        remove_ws_emitter(emitter)
        _logger.exception(
            "event_bus_v2.ws_emitter_evicted",
            reason="raised",
            emitter=_emitter_name(emitter),
            event_type=payload.get("type"),
        )


def _emitter_name(emitter: WsEmitter) -> str:
    """Loggable identity for an emitter callable (eviction context)."""

    return getattr(emitter, "__qualname__", None) or repr(emitter)


def _default_summary(payload: WsTaskEvent) -> str:
    """Best-effort one-line summary derived from the WS payload type."""

    event_type = payload.get("type", "event")
    task_id = payload.get("task_id")
    if isinstance(task_id, str) and task_id:
        return f"{event_type} (task={task_id})"
    return str(event_type)


def _patch_last_event_task_id(task_id: str) -> None:
    """Replace the last buffer entry with a copy carrying ``task_id``.

    Called by :func:`emit_event` when the ContextVar produced ``None``
    for the task id but the WS payload carried one. The swap itself is
    owned by :func:`bob.debug_log.replace_last_event` (issue 0123) so the
    ring buffer's cached retention size stays in step with the patched
    copy. The deque is bounded so this is O(1).

    This is a small wart compared to threading an explicit ``task_id=``
    parameter into :func:`emit_debug`; we chose this path because every
    existing call site of :func:`emit_debug` already reads from the
    ContextVar — extending the signature would risk silently breaking
    callers that pass ``payload`` positional-by-habit. Keeping the
    surface unchanged means 0052 cannot regress any pre-existing emit.
    """

    from bob.debug_log import _buffer  # local import — module-private

    if not _buffer:
        return
    last = _buffer[-1]
    if last.task_id is not None:
        # Already populated by the ContextVar — nothing to do.
        return
    patched = DebugEvent(
        ts=last.ts,
        category=last.category,
        severity=last.severity,
        source=last.source,
        summary=last.summary,
        payload=last.payload,
        turn_id=last.turn_id,
        correlation_id=last.correlation_id,
        parent_task_id=task_id,
        task_id=task_id,
        replayed=last.replayed,
    )
    replace_last_event(patched)


def get_snapshot() -> list[DebugEvent]:
    """Return a copy of the ring buffer — alias for the test surface."""

    return snapshot()


def get_snapshot_for_task(task_id: str) -> list[DebugEvent]:
    """Return the snapshot filtered for a single ``task_id`` (issue 0052)."""

    return snapshot_for_task(task_id)


async def subscribe_for_task(task_id: str) -> AsyncGenerator[DebugEvent, None]:
    """Snapshot-then-tail subscription scoped to a single ``task_id``.

    Yields:
    1. Every currently-buffered event whose ``task_id`` matches, tagged
       with ``replayed=True``.
    2. Then every live event whose ``task_id`` matches, with
       ``replayed=False``.

    This is the producer side of the ``/ws/task/{task_id}`` route. The WS
    handler in :mod:`bob.ws_router` iterates this generator and pushes
    each event to the socket as JSON.

    Multi-client safety: the underlying :func:`subscribe_filtered` creates
    a dedicated per-subscriber queue. Two overlays for different
    ``task_id`` cannot see each other's events because the filter is
    applied in the consumer loop.
    """

    async for event in subscribe_filtered(lambda e: e.task_id == task_id):
        yield event


__all__ = [
    "WsEmitter",
    "WsTaskEvent",
    "add_ws_emitter",
    "emit_event",
    "flush_hot_events",
    "get_snapshot",
    "get_snapshot_for_task",
    "remove_ws_emitter",
    "set_ws_emitter",
    "snapshot_ws_emitters",
    "subscribe_for_task",
]
