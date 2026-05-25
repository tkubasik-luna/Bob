"""WebSocket transport for the debug event feed (PRD 0005, slice 0038).

Thin layer over :mod:`bob.debug_log`: accepts a WS connection on
``/ws/debug``, subscribes to the in-process event stream and forwards
each :class:`bob.debug_log.DebugEvent` as JSON.

The debug feed is intentionally separate from the user-facing
``/ws/chat``:

- the contract is producer-only — the client never sends anything;
- connect / disconnect of this WS does NOT affect the chat session;
- the ring buffer in :mod:`bob.debug_log` keeps accumulating even when
  no debug client is connected, so opening the debug window mid-session
  replays the recent history.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bob.debug_log import subscribe

router = APIRouter()
_logger = structlog.get_logger(__name__)


@router.websocket("/ws/debug")
async def debug_ws(websocket: WebSocket) -> None:
    """Stream every :class:`DebugEvent` to the connected client as JSON.

    Yield order: ring-buffer snapshot first (events tagged ``replayed=True``)
    followed by the live tail. The loop terminates cleanly on
    :class:`WebSocketDisconnect`; the :func:`subscribe` generator's
    ``finally`` clause unregisters the subscriber so we don't leak
    queues across reconnects.
    """

    await websocket.accept()
    try:
        async for event in subscribe():
            try:
                await websocket.send_json(event.to_dict())
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        return
    except Exception:
        # Never let a single bad client kill the producer side. Log and
        # let the generator's finally clean up the subscriber registration.
        _logger.exception("ws_debug.stream_failed")
        return
