"""Thin shim — routes through :mod:`bob.event_bus_v2` (issue 0052).

Pre-0052 this module owned a module-level emitter callable + an
``emit()`` no-op fallback. Sub-agent runners, the scheduler and the
orchestrator all called ``ws_events.emit(payload)`` to push lifecycle
events to the connected chat WS. The debug feed lived in parallel via
:func:`bob.debug_log.emit_debug`.

Issue 0052 collapses the two producers into one. The :mod:`event_bus_v2`
module is the single source of truth: every emit lands in the ring
buffer (so the debug feed AND the per-task overlay see it) and is
forwarded to the registered WS emitter (so the chat client's sidebar
handlers keep working unchanged).

This file is kept as a one-call shim so:

- existing call sites (sub-agent runner, scheduler, orchestrator) don't
  need a global rename in a single commit;
- the test fixtures (``ws_events.set_emitter(...)``) keep working — they
  now register the emitter on the unified bus;
- the public surface ``ws_events.emit(payload)`` stays the same.

Both :func:`set_emitter` and :func:`emit` are thin wrappers that delegate
to :mod:`bob.event_bus_v2`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from bob.event_bus_v2 import add_ws_emitter, emit_event, remove_ws_emitter, set_ws_emitter

TaskEvent = dict[str, Any]


def set_emitter(fn: Callable[[TaskEvent], Awaitable[None]] | None) -> None:
    """Replace the emitter set with a single ``fn`` (or clear on ``None``).

    Back-compat convenience for single-emitter call sites + tests. Live
    multi-window WS handling uses :func:`add_emitter` / :func:`remove_emitter`
    so every open window receives the fan-out.
    """

    set_ws_emitter(fn)


def add_emitter(fn: Callable[[TaskEvent], Awaitable[None]]) -> None:
    """Register a connected session's emitter (fan-out target).

    Each ``/ws/chat`` window registers on connect so task lifecycle events,
    streamed ``speech_delta`` / ``ui_payload`` frames and proactive pushes
    reach every open window, not just the last one to connect.
    """

    add_ws_emitter(fn)


def remove_emitter(fn: Callable[[TaskEvent], Awaitable[None]]) -> None:
    """Unregister a session's emitter on disconnect. Idempotent."""

    remove_ws_emitter(fn)


async def emit(event: TaskEvent, *, debug_event: TaskEvent | None = None) -> None:
    """Route ``event`` through the unified bus (issue 0052).

    Every emit now lands in the debug ring buffer AND is forwarded to
    the WS chat emitter (when registered). The per-task overlay
    subscribes to the same ring buffer via
    :func:`bob.event_bus_v2.subscribe_for_task` so no parallel path is
    required.

    Pass ``debug_event`` (PRD 0008 / issue 0064) with a redacted copy when
    the chat frame carries fields the debug sinks must not see — e.g. the
    ``task_result`` event ships the real Mail ``result_payload`` to the
    overlay while only a scrubbed copy lands in the ring buffer / ``/ws/debug``
    feed / JSONL sink. Defaults to ``event`` so existing callers are
    unchanged.
    """

    await emit_event(event, debug_payload=debug_event)
