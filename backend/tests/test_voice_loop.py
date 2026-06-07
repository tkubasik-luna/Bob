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
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
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
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
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


# --- barge-in (issue 0101) ---------------------------------------------------


def _bargein_events(turn_id: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") != "bargein":
            continue
        if turn_id is not None and ws_event.get("turn_id") != turn_id:
            continue
        out.append(ws_event)
    return out


class _SpeakingSayPath:
    """A say-path that enters bob_speaking, reports played text, then blocks.

    Lets a test drive the loop into ``bob_speaking`` with a known
    ``committed_spoken_text`` and hold it there while it injects the barge-in
    frames. ``release`` lets the (cancelled) task unwind.
    """

    def __init__(self, *, played: str = "Bonjour le monde") -> None:
        self.played = played
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.committed: list[str] = []

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        await on_first_audio()  # → bob_speaking
        if on_spoken_progress is not None:
            await on_spoken_progress(self.played)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _fake_clock(step_ms: float = 30.0) -> Callable[[], float]:
    """A monotonic clock that advances ``step_ms`` per call (deterministic ts)."""

    state = {"t": 0.0}

    def _now() -> float:
        state["t"] += step_ms / 1000.0
        return round(state["t"], 6)

    return _now


def _bargein_loop(say: _SpeakingSayPath, *, confirm_ms: int = 90) -> FullDuplexLoop:
    settings = Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=120,
        STT_DEBUG_TEXT_MAX_CHARS=64,
        BARGEIN_CONFIRM_MS=confirm_ms,
    )
    committed: list[tuple[str, str]] = []
    restarted: list[str] = []

    async def _commit(turn_id: str, text: str) -> None:
        committed.append((turn_id, text))

    async def _restart(turn_id: str) -> None:
        restarted.append(turn_id)

    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        commit_spoken=_commit,
        on_thinker_restart=_restart,
    )
    # A deterministic clock so the confirmation window is crossed by a known
    # number of frames regardless of host speed.
    loop._now = _fake_clock(step_ms=30.0)  # type: ignore[method-assign]
    # Expose the sinks for assertions.
    loop._test_committed = committed  # type: ignore[attr-defined]
    loop._test_restarted = restarted  # type: ignore[attr-defined]
    return loop


async def _drive_to_bob_speaking(loop: FullDuplexLoop, say: _SpeakingSayPath) -> None:
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await say.entered.wait()
    assert loop.state is TurnState.BOB_SPEAKING


async def test_bargein_cuts_and_commits_played_text() -> None:
    say = _SpeakingSayPath(played="Bonjour le monde")
    loop = _bargein_loop(say, confirm_ms=90)
    await _drive_to_bob_speaking(loop, say)
    turn_id = loop._fsm.turn_id

    # Feed continuous voiced frames; with the 30 ms/call fake clock, ~4 frames
    # cross the 90 ms window and confirm the barge-in.
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)

    # The say-path was cancelled and the FSM handed the floor back to the user.
    assert say.cancelled is True
    assert loop.state.value == "user_speaking"
    assert loop._fsm.turn_id == turn_id  # same turn retained

    # committed_spoken_text == what Bob actually played.
    assert loop._test_committed == [(turn_id, "Bonjour le monde")]  # type: ignore[attr-defined]
    # Thinker restart hook fired.
    assert loop._test_restarted == [turn_id]  # type: ignore[attr-defined]

    # A bargein event was emitted with the played text + detected/cut ts.
    bargeins = _bargein_events(turn_id)
    assert len(bargeins) == 1
    ev = bargeins[0]
    assert ev["committed_spoken_text"] == "Bonjour le monde"
    assert ev["cut_ts"] >= ev["detected_ts"]

    # The turn_state carried the barge-in transition.
    transitions = _turn_states(turn_id)
    assert any(t["from"] == "bob_speaking" and t["to"] == "user_speaking" for t in transitions)

    say.release.set()
    await loop.stop()


async def test_bargein_latency_marks_emitted() -> None:
    say = _SpeakingSayPath(played="Salut")
    loop = _bargein_loop(say, confirm_ms=90)
    await _drive_to_bob_speaking(loop, say)
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    say.release.set()
    # Let the resumed turn end so its turn_latency (carrying the barge-in marks)
    # fires: voice_stop teardown emits the summary.
    await loop.stop()

    latencies = _latency_events()
    assert latencies, "expected a turn_latency event"
    # Some emitted latency summary carries the barge-in marks + derived metric.
    with_bargein = [m for m in latencies if "t_bargein_detected" in m["marks"]]
    assert with_bargein, "expected a turn_latency carrying barge-in marks"
    marks = with_bargein[-1]["marks"]
    assert "t_cut" in marks
    assert marks["t_cut"] >= marks["t_bargein_detected"]
    assert "bargein_cut_ms" in with_bargein[-1]["derived"]


async def test_short_burst_during_bob_speaking_does_not_cut() -> None:
    """A backchannel below the confirmation window must NOT interrupt Bob."""

    say = _SpeakingSayPath(played="Bonjour")
    # Window of 200 ms; with 30 ms/call clock, 3 voiced frames (~90 ms) is short.
    loop = _bargein_loop(say, confirm_ms=200)
    await _drive_to_bob_speaking(loop, say)

    # Two short voiced bursts separated by silence (each below the window).
    await loop.feed_raw_frame(_LOUD)
    await loop.feed_raw_frame(_LOUD)
    await loop.feed_raw_frame(_QUIET)  # resets the run
    await loop.feed_raw_frame(_LOUD)
    await loop.feed_raw_frame(_LOUD)

    # Bob still holds the floor — no cut, no commit, no bargein event.
    assert loop.state is TurnState.BOB_SPEAKING
    assert say.cancelled is False
    assert loop._test_committed == []  # type: ignore[attr-defined]
    assert _bargein_events() == []

    say.release.set()
    await loop.stop()
