"""Unit tests for the backend voice-turn orchestration (issue 0099).

Drives :class:`bob.voice_turn.VoiceTurn` with the deterministic fake STT
engine and asserts:

- ``stt_partial`` / ``stt_final`` are emitted with ``turn_id`` correlation and
  ``stable_prefix_len`` on partials (Annexe A.2);
- the ring-buffer (debug) copy of the transcript is SCRUBBED while the client
  payload carries the full text (Privacy note);
- degradation (Annexe G): an engine that fails to open aborts the turn cleanly
  (``voice_turn_error`` / ``end_reason:error``) and never raises;
- the lazy-download path emits a ``stt_preparing`` → ``stt_ready`` toast pair.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import Any

import pytest

from bob import event_bus_v2
from bob.debug_log import current_turn_id, snapshot
from bob.stt_engine import SttEngineUnavailableError, SttFinal, SttPartial
from bob.voice_turn import VoiceTurn


class _CaptureBus:
    """Capture every WS payload + ring-buffer the debug copies."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def emit(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)

    def of_type(self, t: str) -> list[dict[str, Any]]:
        return [p for p in self.payloads if p.get("type") == t]


@pytest.fixture()
def bus() -> Iterator[_CaptureBus]:
    cap = _CaptureBus()
    event_bus_v2.set_ws_emitter(cap.emit)
    token = current_turn_id.set(None)
    try:
        yield cap
    finally:
        event_bus_v2.set_ws_emitter(None)
        current_turn_id.reset(token)


class _ScriptedSession:
    def __init__(
        self, partials: list[SttPartial], final: str, *, raise_on_frame: int | None
    ) -> None:
        self._partials = partials
        self._final = final
        self._raise_on = raise_on_frame
        self._n = 0

    def accept_frame(self, pcm: bytes) -> list[SttPartial]:
        if self._raise_on is not None and self._n == self._raise_on:
            raise RuntimeError("boom mid-turn")
        out = [self._partials[self._n]] if self._n < len(self._partials) else []
        self._n += 1
        return out

    def finalize(self) -> SttFinal:
        return SttFinal(text=self._final)

    def close(self) -> None:
        return None


class _ScriptedEngine:
    def __init__(
        self,
        *,
        partials: list[SttPartial] | None = None,
        final: str = "",
        cached: bool = True,
        open_raises: Exception | None = None,
        preload_raises: Exception | None = None,
        raise_on_frame: int | None = None,
    ) -> None:
        self._partials = partials or []
        self._final = final
        self._cached = cached
        self._open_raises = open_raises
        self._preload_raises = preload_raises
        self._raise_on_frame = raise_on_frame
        self.preloaded = False

    def open_session(self, turn_id: str) -> _ScriptedSession:
        if self._open_raises is not None:
            raise self._open_raises
        return _ScriptedSession(self._partials, self._final, raise_on_frame=self._raise_on_frame)

    def is_model_cached(self) -> bool:
        return self._cached

    def preload(self) -> None:
        if self._preload_raises is not None:
            raise self._preload_raises
        self.preloaded = True


_PCM = struct.pack("<8h", *([0] * 8))


async def test_emits_partials_and_final_with_turn_id(bus: _CaptureBus) -> None:
    engine = _ScriptedEngine(
        partials=[SttPartial("bonjour", 0), SttPartial("bonjour paris", 7)],
        final="bonjour paris",
    )
    turn = VoiceTurn(engine=engine, session_id="s1")
    assert await turn.start() is True

    await turn.feed_frame(_PCM)
    await turn.feed_frame(_PCM)
    final = await turn.finalize()

    partials = bus.of_type("stt_partial")
    assert [p["text"] for p in partials] == ["bonjour", "bonjour paris"]
    assert all(p["turn_id"] == turn.turn_id for p in partials)
    assert partials[1]["stable_prefix_len"] == 7
    assert "ts" in partials[0]

    finals = bus.of_type("stt_final")
    assert len(finals) == 1
    assert finals[0]["text"] == "bonjour paris"
    assert finals[0]["turn_id"] == turn.turn_id
    assert final is not None and final.text == "bonjour paris"


async def test_debug_copy_is_scrubbed_while_client_gets_full_text(bus: _CaptureBus) -> None:
    long_text = "ceci est un transcript assez long qui doit etre tronque"
    engine = _ScriptedEngine(partials=[SttPartial(long_text, 0)], final=long_text)
    turn = VoiceTurn(engine=engine, session_id="s2")
    await turn.start()
    await turn.feed_frame(_PCM)
    await turn.finalize()

    # Client payload (captured emitter) carries the FULL text.
    client_final = bus.of_type("stt_final")[0]
    assert client_final["text"] == long_text

    # Ring-buffer (debug) copy is the scrubbed/truncated text.
    voice_events = [e for e in snapshot() if e.category == "voice"]
    debug_finals = [
        e for e in voice_events if e.payload.get("ws_event", {}).get("type") == "stt_final"
    ]
    assert debug_finals, "stt_final must land in the ring buffer"
    debug_text = debug_finals[-1].payload["ws_event"]["text"]
    assert debug_text != long_text
    assert len(debug_text) < len(long_text)
    # Ring-buffer event is correlated to the turn.
    assert debug_finals[-1].turn_id == turn.turn_id


async def test_engine_unavailable_aborts_cleanly(bus: _CaptureBus) -> None:
    engine = _ScriptedEngine(open_raises=SttEngineUnavailableError("no pywhispercpp"))
    turn = VoiceTurn(engine=engine, session_id="s3")

    # start() returns False and never raises (Annexe G).
    assert await turn.start() is False
    errors = bus.of_type("voice_turn_error")
    assert len(errors) == 1
    assert errors[0]["end_reason"] == "error"
    # Feeding frames after a failed start is a silent no-op.
    await turn.feed_frame(_PCM)
    assert bus.of_type("stt_partial") == []


async def test_stt_failure_mid_turn_aborts_cleanly(bus: _CaptureBus) -> None:
    engine = _ScriptedEngine(
        partials=[SttPartial("ok", 0)],
        final="ok",
        raise_on_frame=1,  # second frame raises
    )
    turn = VoiceTurn(engine=engine, session_id="s4")
    await turn.start()
    await turn.feed_frame(_PCM)  # emits one partial
    await turn.feed_frame(_PCM)  # raises -> clean abort

    assert len(bus.of_type("stt_partial")) == 1
    errors = bus.of_type("voice_turn_error")
    assert len(errors) == 1
    assert errors[0]["end_reason"] == "error"
    # No stt_final on the error path.
    assert bus.of_type("stt_final") == []


async def test_lazy_download_emits_preparing_then_ready(bus: _CaptureBus) -> None:
    engine = _ScriptedEngine(partials=[], final="x", cached=False)
    turn = VoiceTurn(engine=engine, session_id="s5")
    assert await turn.start() is True
    assert engine.preloaded is True

    types = [p["type"] for p in bus.payloads]
    assert types.index("stt_preparing") < types.index("stt_ready")


async def test_download_failure_aborts_before_session(bus: _CaptureBus) -> None:
    engine = _ScriptedEngine(cached=False, preload_raises=RuntimeError("net down"))
    turn = VoiceTurn(engine=engine, session_id="s6")
    assert await turn.start() is False
    assert bus.of_type("stt_preparing")  # toast was shown
    assert not bus.of_type("stt_ready")  # never got ready
    assert bus.of_type("voice_turn_error")[0]["end_reason"] == "error"


async def test_finalize_is_idempotent(bus: _CaptureBus) -> None:
    engine = _ScriptedEngine(partials=[], final="done")
    turn = VoiceTurn(engine=engine, session_id="s7")
    await turn.start()
    first = await turn.finalize()
    second = await turn.finalize()
    assert first is not None and first.text == "done"
    assert second is None
    assert len(bus.of_type("stt_final")) == 1


async def test_bad_binary_frame_is_dropped_not_aborted(bus: _CaptureBus) -> None:
    engine = _ScriptedEngine(partials=[SttPartial("hi", 0)], final="hi")
    turn = VoiceTurn(engine=engine, session_id="s8")
    await turn.start()
    # Wrong tag -> decode_pcm_frame raises SttFrameError -> frame dropped.
    await turn.feed_raw_frame(bytes([0x02, 0x00, 0x00]))
    assert bus.of_type("stt_partial") == []
    assert bus.of_type("voice_turn_error") == []
    # A good frame still works afterwards.
    await turn.feed_raw_frame(bytes([0x01]) + _PCM)
    assert len(bus.of_type("stt_partial")) == 1
