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
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import websockets

from bob.stt_engine import MIC_FRAME_TAG

CapturedEvent = dict[str, Any]

#: Default mic frame duration. 30 ms at 16 kHz = 480 samples = 960 bytes — the
#: same frame size the webview ``MicCapture`` worklet produces (Annexe A.1).
DEFAULT_FRAME_MS = 30


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


#: First byte of a mic frame (Annexe A.1) — single-sourced from the STT engine.
_MIC_FRAME_TAG = bytes([MIC_FRAME_TAG])


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolate ``samples`` from ``src_rate`` to ``dst_rate``.

    Good enough for a test fixture (we are not chasing audio fidelity, just a
    16 kHz mono stream the fake engine counts and the decoder validates). A
    no-op when the rates already match.
    """

    if src_rate == dst_rate or samples.size == 0:
        return samples
    duration = samples.size / float(src_rate)
    dst_n = max(1, round(duration * dst_rate))
    src_idx = np.linspace(0.0, samples.size - 1, num=dst_n)
    resampled = np.interp(src_idx, np.arange(samples.size), samples)
    return np.asarray(resampled, dtype=np.float32)


def wav_to_pcm16_frames(
    path: str | Path,
    *,
    target_rate: int = 16_000,
    frame_ms: int = DEFAULT_FRAME_MS,
) -> list[bytes]:
    """Read a WAV fixture → list of binary mic frames (tag ``0x01``).

    Converts to mono, resamples to ``target_rate``, re-quantises to s16le, and
    slices into ``frame_ms`` frames each prefixed with the ``0x01`` mic tag —
    the exact bytes the webview ``MicCapture`` ships (Annexe A.1). Supports
    8/16/32-bit PCM WAVs. Used by the ``--audio`` real-engine attest path; the
    deterministic fake path uses :func:`synth_mic_frames` instead.
    """

    path = Path(path)
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        src_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sampwidth == 1:
        # 8-bit WAV is unsigned; center to [-1, 1).
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV sample width: {sampwidth} bytes")

    if n_channels > 1:
        arr = arr.reshape(-1, n_channels).mean(axis=1)

    arr = _resample_linear(arr, src_rate, target_rate)
    pcm16 = np.clip(arr, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype("<i2")

    samples_per_frame = max(1, int(target_rate * frame_ms / 1000))
    frames: list[bytes] = []
    for start in range(0, pcm16.size, samples_per_frame):
        chunk = pcm16[start : start + samples_per_frame]
        if chunk.size == 0:
            continue
        frames.append(_MIC_FRAME_TAG + chunk.tobytes())
    return frames


def synth_mic_frames(*, frame_count: int = 8, samples_per_frame: int = 480) -> list[bytes]:
    """Synthesise ``frame_count`` silent mic frames (tag ``0x01`` + s16le zeros).

    The deterministic fake STT engine ignores PCM *content* (it converges to a
    scripted transcript), so silent frames are enough to exercise the REAL
    binary-decode → ``VoiceTurn`` → ``stt_final`` path end-to-end over the wire.
    ``480`` samples = 30 ms at 16 kHz, the same frame size the webview worklet
    produces.
    """

    pcm = b"\x00\x00" * samples_per_frame  # s16le silence
    return [_MIC_FRAME_TAG + pcm for _ in range(max(1, frame_count))]


async def inject_audio_ws(
    ws_base: str,
    frames: list[bytes],
    *,
    settle_ms: int = 300,
    open_timeout_ms: int = 10000,
) -> None:
    """Drive one voice turn through the real binary ``/ws/chat`` path (0099).

    Opens a chat socket, drains the connect-time ``session`` frame, sends
    ``voice_start`` (JSON), streams the binary mic ``frames`` (each tag ``0x01``
    + s16le PCM), then ``voice_stop`` (JSON) which freezes the turn server-side
    and emits ``stt_final`` on ``/ws/debug``. Returns after a short settle so the
    server finalises before the socket closes. The ``/ws/debug`` capture (a
    separate socket) records the emitted ``stt_partial`` / ``stt_final`` frames;
    the caller's ``wait_event`` / assertions read them there.
    """

    async with websockets.connect(
        f"{ws_base}/ws/chat", open_timeout=open_timeout_ms / 1000.0
    ) as conn:
        # Drain the connect-time ``session`` frame (and any replay) briefly.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(conn.recv(), timeout=2.0)

        await conn.send(json.dumps({"type": "voice_start", "window": "new"}))
        for frame in frames:
            await conn.send(frame)
        await conn.send(json.dumps({"type": "voice_stop"}))
        # Let the server process voice_stop → finalize → emit stt_final before
        # the socket closes (close also finalises, but an explicit settle keeps
        # the event ordering deterministic for the capture).
        await asyncio.sleep(max(settle_ms, 0) / 1000.0)
