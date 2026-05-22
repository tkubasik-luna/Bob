"""Module-level WS event broadcaster for orchestrator / sub-agent → frontend.

Background sub-agent runs do not own a WebSocket — but they need to push state
transitions to the frontend (slice #0019). Threading a websocket reference all
the way down through ``Orchestrator`` and ``SubAgentRunner`` would couple
domain layers to the transport.

Instead, the WS handler in :mod:`bob.ws_router` registers a session-scoped
emitter callable for the duration of the connection, and the orchestrator
plus sub-agent runner simply call :func:`emit` whenever a task event must be
pushed. When no emitter is installed (e.g. unit tests for the orchestrator
in isolation), :func:`emit` is a no-op.

Design constraints:

- Single emitter at a time — Bob is a single-user desktop app, exactly one
  WebSocket is connected at any moment. Connecting a second WS implicitly
  replaces the previous emitter via :func:`set_emitter`.
- Async-only: the emitter is awaited so it composes naturally with
  ``websocket.send_json`` on the FastAPI side.
- The payload shape is the responsibility of the caller — this module only
  forwards the dict verbatim.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

TaskEvent = dict[str, Any]

_emitter: Callable[[TaskEvent], Awaitable[None]] | None = None


def set_emitter(fn: Callable[[TaskEvent], Awaitable[None]] | None) -> None:
    """Install (or clear) the process-wide async emitter.

    Passing ``None`` clears the emitter — :func:`emit` becomes a no-op until a
    new emitter is registered.
    """

    global _emitter
    _emitter = fn


async def emit(event: TaskEvent) -> None:
    """Forward ``event`` to the registered emitter; no-op when none is set."""

    if _emitter is None:
        return
    await _emitter(event)
