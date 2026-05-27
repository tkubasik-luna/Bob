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
``async`` and may itself block on a slow socket — but the WS layer owns
that emitter and only registers it for the session lifetime.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import structlog

from bob.debug_log import (
    DebugCategory,
    DebugEvent,
    DebugSeverity,
    current_task_id,
    emit_debug,
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


async def emit_event(
    payload: WsTaskEvent,
    *,
    category: DebugCategory = "task",
    severity: DebugSeverity = "info",
    source: str = "bob.event_bus_v2.emit_event",
    summary: str | None = None,
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

    Concurrency: the debug emit is synchronous; the WS forward is awaited
    so a slow socket can be back-pressured by the caller (matches the
    legacy :func:`bob.ws_events.emit` contract).
    """

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

    effective_summary = summary or _default_summary(payload)
    emit_debug(
        category=category,
        severity=severity,
        source=source,
        summary=effective_summary,
        payload={"ws_event": payload},
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

    if not _ws_emitters:
        return
    # Snapshot the set: a forwarder may disconnect (and remove itself) while we
    # await a slow sibling. Each forward is isolated so one dead/slow socket
    # cannot starve the other windows.
    for emitter in list(_ws_emitters):
        try:
            await emitter(payload)
        except Exception:
            _logger.exception(
                "event_bus_v2.ws_forward_failed",
                event_type=payload.get("type"),
            )


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
    for the task id but the WS payload carried one. The ring buffer is
    a :class:`collections.deque`; we pop the last entry, build a copy
    with the resolved id and re-append. The deque is bounded so this
    is O(1).

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
    _buffer[-1] = patched


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
    "get_snapshot",
    "get_snapshot_for_task",
    "remove_ws_emitter",
    "set_ws_emitter",
    "subscribe_for_task",
]
