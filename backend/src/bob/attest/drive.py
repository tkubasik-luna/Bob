"""Black-box WS drive layer for the attestation harness.

PRD 0016 / issue 0098. :class:`DebugCapture` and :func:`inject_text` talk to a
*running* backend over the SAME public WebSockets the frontend uses — never via
in-process internals. That is the whole point of "black-box on the real
WS/HTTP": whatever the harness attests is true of the wire contract.

- :class:`DebugCapture` connects to ``/ws/debug`` and drains every
  :class:`bob.debug_log.DebugEvent` frame into an in-memory list, exposing a
  :meth:`wait_for` coroutine the timeline uses for ``wait_event``.
- :func:`inject_text` opens ``/ws/chat``, sends one ``user_msg`` (the existing
  ``client_text`` path — the runner injects a transcript, skipping STT) and
  returns once the turn's ``assistant_msg`` + ``thinking:end`` have come back,
  so the caller knows the turn is fully processed before asserting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import websockets

CapturedEvent = dict[str, Any]


class DebugCapture:
    """Stream + buffer every ``/ws/debug`` frame for the life of a run.

    Start with :meth:`open` (awaits the WS handshake + the first snapshot
    drain), stop with :meth:`close`. Captured frames are available via
    :attr:`events`; :meth:`wait_for` blocks until a frame satisfying a
    predicate arrives (or a timeout elapses) so ``wait_event`` can synchronise
    on a logical event without polling internals.
    """

    def __init__(self, ws_base: str) -> None:
        self._url = f"{ws_base}/ws/debug"
        self._events: list[CapturedEvent] = []
        self._conn: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._new_event = asyncio.Event()

    @property
    def events(self) -> list[CapturedEvent]:
        """A snapshot copy of every frame captured so far (arrival order)."""

        return list(self._events)

    async def open(self) -> None:
        """Connect and start draining frames in a background task."""

        self._conn = await websockets.connect(self._url, open_timeout=10)
        self._reader_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        conn = self._conn
        if conn is None:  # pragma: no cover — open() always sets it first.
            return
        try:
            async for raw in conn:
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(frame, dict):
                    self._events.append(frame)
                    self._new_event.set()
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            return
        except Exception:
            # A dead capture socket must not crash the run — the verdict will
            # simply reflect whatever events were captured before the break.
            return

    async def wait_for(
        self, predicate: Callable[[CapturedEvent], bool], *, timeout_ms: int
    ) -> bool:
        """Return True as soon as a captured frame satisfies ``predicate``.

        Checks the already-buffered frames first (the event may have arrived
        before the wait started), then waits for new frames until the timeout.
        Returns False on timeout. Never raises on timeout — the caller turns a
        False into a timeline note / assertion failure.
        """

        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
        while True:
            if any(predicate(event) for event in self._events):
                return True
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            self._new_event.clear()
            try:
                await asyncio.wait_for(self._new_event.wait(), timeout=remaining)
            except TimeoutError:
                return False

    async def close(self) -> None:
        """Cancel the reader and close the socket. Idempotent."""

        if self._reader_task is not None:
            self._reader_task.cancel()
            # CancelledError is a BaseException in 3.12 (not caught by
            # ``Exception``); suppress both so a torn-down reader never raises.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None


async def inject_text(ws_base: str, text: str, *, turn_timeout_ms: int = 30000) -> None:
    """Drive one text turn through the real ``/ws/chat`` ``user_msg`` path.

    Opens a fresh chat socket, sends ``{"type": "user_msg", "content": text}``
    (voice intentionally off — the harness injects a transcript and skips TTS),
    then reads frames until the turn completes (``thinking:end`` after the
    ``assistant_msg``) so the debug events for the turn are guaranteed to have
    been emitted before the caller proceeds. The socket is closed on exit.

    A turn that errors still emits ``thinking:end`` (see
    :func:`bob.ws_router._handle_client_message`), so this returns on both the
    happy and the error path — the assertions then judge the captured events.
    """

    async with websockets.connect(f"{ws_base}/ws/chat", open_timeout=10) as conn:
        deadline = asyncio.get_event_loop().time() + (turn_timeout_ms / 1000.0)

        async def _recv() -> dict[str, Any]:
            remaining = deadline - asyncio.get_event_loop().time()
            raw = await asyncio.wait_for(conn.recv(), timeout=max(remaining, 0.001))
            frame = json.loads(raw)
            return frame if isinstance(frame, dict) else {}

        # Drain the connect-time frames (``session`` + any history/task replay)
        # until the socket is quiet, then inject. The session frame is always
        # first; replay frames only exist on a primed store (never on the fresh
        # ephemeral DB), so in practice this reads exactly one frame.
        first = await _recv()
        _ = first  # session frame — not asserted here.

        await conn.send(json.dumps({"type": "user_msg", "content": text}))

        # Read until the turn's terminal ``thinking: end``. The ordering is
        # thinking:start → (assistant_msg | error) → thinking:end.
        saw_thinking_end = False
        while not saw_thinking_end:
            frame = await _recv()
            if frame.get("type") == "thinking" and frame.get("state") == "end":
                saw_thinking_end = True
