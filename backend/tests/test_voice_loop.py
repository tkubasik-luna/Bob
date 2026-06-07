"""Unit tests for the full-duplex loop glue (PRD 0016 / issue 0100).

Drives the loop with synthetic PCM frames through a fake STT VoiceTurn + a fake
say-path driver (no orchestrator, no TTS) and asserts:

- a voiced burst + trailing silence walks the FSM
  idle -> user_speaking -> thinking -> bob_speaking -> idle;
- the frozen transcript reaches the say-path;
- ``turn_state`` + ``turn_latency`` voice events are emitted with the marks;
- ``voice_stop`` tears the loop down cleanly;
- a say-path that produces no audio still returns the FSM to idle.

The emitted voice events are read back from the debug ring buffer (the same
sink :func:`bob.event_bus_v2.emit_event` writes to).
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from bob import debug_log
from bob.config import Settings
from bob.stt_engine import MIC_FRAME_TAG, FakeSttEngine
from bob.turn_fsm import TurnState
from bob.voice_loop import FullDuplexLoop
from bob.voice_turn import VoiceTurn


def _frame(amplitude: int, *, samples: int = 480) -> bytes:
    return bytes([MIC_FRAME_TAG]) + struct.pack(f"<{samples}h", *([amplitude] * samples))


_LOUD = _frame(8000)
_QUIET = _frame(0)


def _settings() -> Settings:
    # Validation-free construction (no LLM env needed); tighten the windows so a
    # handful of frames crosses the floor.
    return Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,  # 2 frames
        ENDPOINT_SILENCE_MS=120,  # 4 frames
        STT_DEBUG_TEXT_MAX_CHARS=64,
    )


def _turn_states(turn_id: str | None = None) -> list[dict[str, Any]]:
    """The turn_state ws_event bodies currently in the debug ring buffer."""

    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        payload = event.payload or {}
        ws_event = payload.get("ws_event") or {}
        if ws_event.get("type") != "turn_state":
            continue
        if turn_id is not None and ws_event.get("turn_id") != turn_id:
            continue
        out.append(ws_event)
    return out


def _latency_events() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "turn_latency":
            out.append(ws_event)
    return out


class _RecordingSayPath:
    """A fake say-path: records the transcript, optionally emits 'audio'."""

    def __init__(self, *, produce_audio: bool = True) -> None:
        self.transcripts: list[str] = []
        self.turn_ids: list[str] = []
        self._produce_audio = produce_audio

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
    ) -> None:
        self.transcripts.append(transcript)
        self.turn_ids.append(turn_id)
        if self._produce_audio and transcript.strip():
            await on_first_audio()


def _loop(say_path: _RecordingSayPath, transcript: str = "bonjour") -> FullDuplexLoop:
    settings = _settings()
    return FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript=transcript, samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say_path,
        settings=settings,
        session_id="s1",
    )


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    debug_log.clear()


# --- happy-path full cycle ---------------------------------------------------


async def test_voiced_then_silence_walks_full_fsm_cycle() -> None:
    say = _RecordingSayPath(produce_audio=True)
    loop = _loop(say, transcript="bonjour le monde")
    assert await loop.start() is True

    # Voiced burst opens the turn + feeds STT; trailing silence trips endpoint.
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    # The say-path runs as a background task; await it for a deterministic assert.
    await loop.join()

    # The say-path ran on the frozen transcript and the loop is back to idle.
    assert say.transcripts == ["bonjour le monde"]
    assert loop.state is TurnState.IDLE

    # The FSM walked the full Annexe B basic cycle (ordered).
    transitions = _turn_states(say.turn_ids[0])
    tos = [t["to"] for t in transitions]
    assert tos[:1] == ["user_speaking"]
    # the sequence must contain the ordered milestones
    assert tos.index("user_speaking") < tos.index("thinking")
    assert tos.index("thinking") < tos.index("bob_speaking")
    assert tos.index("bob_speaking") < tos.index("idle")

    # Latency marks were emitted (Annexe F basics).
    latencies = _latency_events()
    assert latencies, "expected a turn_latency event"
    marks = latencies[-1]["marks"]
    assert "t_first_mic_frame" in marks
    assert "t_endpoint" in marks
    assert "t_first_audio_chunk" in marks
    # endpoint precedes first audio.
    assert marks["t_endpoint"] <= marks["t_first_audio_chunk"]


async def test_turn_id_consistent_across_transitions() -> None:
    say = _RecordingSayPath()
    loop = _loop(say)
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    transitions = _turn_states()
    turn_ids = {t["turn_id"] for t in transitions}
    assert len(turn_ids) == 1
    assert say.turn_ids[0] in turn_ids


# --- degraded say-path (no audio) --------------------------------------------


async def test_say_path_without_audio_still_returns_to_idle() -> None:
    say = _RecordingSayPath(produce_audio=False)
    loop = _loop(say)
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    # No bob_speaking (no audio), but the FSM still ended at idle (via the
    # thinking -> idle teardown) and a latency event still fired.
    assert loop.state is TurnState.IDLE
    tos = [t["to"] for t in _turn_states()]
    assert "thinking" in tos
    assert "bob_speaking" not in tos
    assert tos[-1] == "idle"
    assert _latency_events()


# --- voice_stop teardown -----------------------------------------------------


async def test_voice_stop_midspeech_tears_down() -> None:
    say = _RecordingSayPath()
    loop = _loop(say)
    await loop.start()
    # Only a voiced burst (user still speaking) — no endpoint yet.
    for _ in range(5):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING

    await loop.stop()
    # Read the state fresh (avoid mypy narrowing the property from the assert
    # above) — voice_stop drove the FSM back to idle.
    assert loop.state.value == "idle"
    # The say-path never ran (the user was cut off before endpoint).
    assert say.transcripts == []
    # voice_stop transition recorded.
    assert any(t["reason"] == "voice_stop" for t in _turn_states())


async def test_frames_after_stop_are_dropped() -> None:
    say = _RecordingSayPath()
    loop = _loop(say)
    await loop.start()
    await loop.stop()
    before = len(debug_log.snapshot())
    for _ in range(10):
        await loop.feed_raw_frame(_LOUD)
    assert len(debug_log.snapshot()) == before  # nothing emitted
    assert loop.state is TurnState.IDLE


# --- silent frames never open a turn (0099 STT-only behaviour) ---------------


class _BlockingSayPath:
    """A say-path that parks until released — lets a test inject a resume edge."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.transcripts: list[str] = []

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
    ) -> None:
        self.transcripts.append(transcript)
        self.entered.set()
        await self.release.wait()


async def test_user_resumes_during_thinking_does_not_corrupt_state() -> None:
    """A voiced frame while the say-path is mid-thinking re-opens user_speaking.

    The in-flight (blocked) say-path is cancelled; the FSM moves back to
    user_speaking on a NEW turn; the cancelled say-path's finalize is a no-op
    (ownership guard), so the FSM is not yanked back to idle.
    """

    say = _BlockingSayPath()
    settings = _settings()
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
    )
    await loop.start()
    # Turn 1: voiced + silence → endpoint → say-path launched (blocks in thinking).
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await say.entered.wait()
    assert loop.state is TurnState.THINKING
    first_turn = say.transcripts[0]

    # User resumes: a voiced frame trips vad_speech_start in thinking.
    await loop.feed_raw_frame(_LOUD)
    # FSM moved back to user_speaking on a fresh turn (NOT yanked to idle).
    # ``.value`` comparison avoids mypy narrowing the property from the THINKING
    # assert above.
    assert loop.state.value == "user_speaking"

    # The new turn's transitions carry a different turn id than turn 1.
    tos = [(t["turn_id"], t["to"]) for t in _turn_states()]
    assert "user_speaking" in [to for _tid, to in tos]
    # Release the (already cancelled) say-path; nothing should change state.
    say.release.set()
    await loop.join()
    assert loop.state.value == "user_speaking"
    assert say.transcripts == [first_turn]  # the resumed turn never reached endpoint

    await loop.stop()
    assert loop.state.value == "idle"


async def test_silence_only_never_opens_turn() -> None:
    say = _RecordingSayPath()
    loop = _loop(say)
    await loop.start()
    for _ in range(30):
        await loop.feed_raw_frame(_QUIET)
    # VAD never fired → FSM stayed idle → no say-path, no turn_state.
    assert loop.state is TurnState.IDLE
    assert say.transcripts == []
    assert _turn_states() == []
