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
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from bob import debug_log, event_bus_v2, turn_metrics
from bob.config import Settings
from bob.live_transcript_state import LiveTranscriptState
from bob.speculative_draft import SpeculativeDraft
from bob.speech_pipeline import SpeechStreamPipeline
from bob.stt_engine import MIC_FRAME_TAG, FakeSttEngine
from bob.thinker_loop import ThinkerLoop
from bob.tts_service import SynthesisChunk
from bob.turn_fsm import TurnState
from bob.voice_loop import FullDuplexLoop, PersistedTurn
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
        BACKCHANNEL_MIN_INTERVAL_MS=0,  # no refractory in unit tests (issue 0105)
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
    """A fake say-path: records the transcript, optionally emits 'audio'.

    When ``produce_audio`` is set it flips the FSM into ``bob_speaking`` via
    ``on_first_audio``, optionally streams ``audio_chunk_bytes`` of synthetic
    PCM through ``on_audio_chunk`` (issue 0109 ``tts_out`` capture), and reports
    the spoken text via ``on_spoken_progress``.
    """

    def __init__(self, *, produce_audio: bool = True, audio_chunk_bytes: int = 0) -> None:
        self.transcripts: list[str] = []
        self.turn_ids: list[str] = []
        #: The ``prepared_reply`` (committed speculative draft, issue 0104) the loop
        #: passed for each call — ``None`` on the cold path.
        self.prepared_replies: list[str | None] = []
        self._produce_audio = produce_audio
        self._audio_chunk_bytes = audio_chunk_bytes

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        self.transcripts.append(transcript)
        self.turn_ids.append(turn_id)
        self.prepared_replies.append(prepared_reply)
        if self._produce_audio and transcript.strip():
            await on_first_audio()
            if self._audio_chunk_bytes and on_audio_chunk is not None:
                await on_audio_chunk(b"\x00" * self._audio_chunk_bytes, 24_000)
            if on_spoken_progress is not None:
                await on_spoken_progress(transcript)


def _loop(
    say_path: _RecordingSayPath,
    transcript: str = "bonjour",
    *,
    persist_turn: Callable[[Any], Awaitable[None]] | None = None,
    backchannel_trigger: Callable[[], str | None] | None = None,
    backchannel_tts: Callable[[str, str], Awaitable[None]] | None = None,
) -> FullDuplexLoop:
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
        persist_turn=persist_turn,
        backchannel_trigger=backchannel_trigger,
        backchannel_tts=backchannel_tts,
    )


def _backchannel_events() -> list[dict[str, Any]]:
    """The ``backchannel`` ws_event bodies currently in the debug ring buffer."""

    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "backchannel":
            out.append(ws_event)
    return out


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    debug_log.clear()
    # Fresh per-test metrics collector (issue 0117) so rolling aggregates and
    # in-flight turn entries never leak across tests.
    turn_metrics.set_default_collector(None)


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


# --- semantic endpoint (issue 0103, Annexe B + H) ----------------------------


def _semantic_settings() -> Settings:
    # A LARGE silence floor so a turn can ONLY end early via the semantic source;
    # if the silence floor ever fired in these tests it would mask the signal.
    return Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=3_000,  # 100 frames — far beyond what these tests feed
        STT_DEBUG_TEXT_MAX_CHARS=64,
    )


def _semantic_loop(
    say: _RecordingSayPath,
    complete_flag: Callable[[], bool],
    *,
    transcript: str = "bonjour le monde",
) -> FullDuplexLoop:
    settings = _semantic_settings()
    return FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript=transcript, samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        thinker_complete=complete_flag,
    )


async def test_semantic_complete_clause_fires_endpoint_before_floor() -> None:
    # The Thinker flags the clause complete; the fake STT's advancing stable
    # prefix confirms it; the endpoint fires on the FIRST silence frame — far
    # before the 100-frame silence floor would.
    say = _RecordingSayPath(produce_audio=True)
    loop = _semantic_loop(say, lambda: True)
    assert await loop.start() is True

    # Voiced burst opens the turn + reveals the whole transcript (advancing
    # stable prefixes). thinker_complete=True arms the semantic endpoint.
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING
    # ONE silence frame is enough for the confirmed semantic endpoint (vs 100
    # frames for the floor) — proving the early fire.
    await loop.feed_raw_frame(_QUIET)
    await loop.join()

    assert say.transcripts == ["bonjour le monde"]
    tos = [t["to"] for t in _turn_states()]
    assert "thinking" in tos  # the endpoint froze the turn → thinking
    # The endpoint fired on the single trailing silence frame.
    latencies = _latency_events()
    assert latencies and "t_endpoint" in latencies[-1]["marks"]


async def test_no_semantic_signal_falls_back_to_silence_floor() -> None:
    # thinker_complete always False (the Thinker never flags completeness): the
    # semantic source never arms, so the turn ends ONLY via the silence floor.
    # With the large floor, a short trailing silence must NOT end the turn.
    say = _RecordingSayPath(produce_audio=True)
    loop = _semantic_loop(say, lambda: False)
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    # A handful of silence frames — far short of the 100-frame floor.
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)

    # No endpoint yet: the say-path never ran, the FSM is still user_speaking.
    assert say.transcripts == []
    assert loop.state is TurnState.USER_SPEAKING
    await loop.stop()


async def test_midsentence_hesitation_holds_then_floor_eventually_fires() -> None:
    # The Thinker stays silent (incomplete clause). The loop must NOT end the
    # turn early on a pause; the silence floor (here tightened) is the only net.
    settings = Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=150,  # 5 frames
        STT_DEBUG_TEXT_MAX_CHARS=64,
    )
    say = _RecordingSayPath(produce_audio=True)
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour le monde", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        thinker_complete=lambda: False,  # never complete
    )
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    # 4 silence frames < the 5-frame floor → Bob still waits (no premature end).
    for _ in range(4):
        await loop.feed_raw_frame(_QUIET)
    assert loop.state is TurnState.USER_SPEAKING
    assert say.transcripts == []
    # The 5th silence frame crosses the floor — the net still fires.
    await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert say.transcripts == ["bonjour le monde"]


async def test_withdrawn_signal_holds_until_silence_floor() -> None:
    # The Thinker first flags complete, then WITHDRAWS it (a later pass says
    # not-done — a genuine resume). The semantic endpoint disarms and only the
    # silence floor can end the turn.
    settings = Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=180,  # 6 frames
        STT_DEBUG_TEXT_MAX_CHARS=64,
    )
    complete = {"v": True}
    say = _RecordingSayPath(produce_audio=True)
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour le monde", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        thinker_complete=lambda: complete["v"],
    )
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    # The Thinker withdraws BEFORE any silence frame would fire the endpoint.
    complete["v"] = False
    # 5 silence frames < the 6-frame floor → still held (semantic withdrawn).
    for _ in range(5):
        await loop.feed_raw_frame(_QUIET)
    assert loop.state is TurnState.USER_SPEAKING
    assert say.transcripts == []
    # The 6th silence frame crosses the floor — the net fires.
    await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert say.transcripts == ["bonjour le monde"]


# --- semantic bit push (PRD 0018 / issue 0120) --------------------------------


def _push_loop(say: _RecordingSayPath) -> FullDuplexLoop:
    # NO ``thinker_complete`` poll wired: the out-of-band ``note_thinker_complete``
    # push is the ONLY semantic channel, and the 100-frame silence floor the only
    # other way the turn could end. ~1 word revealed per 480-sample frame so the
    # stable prefix keeps advancing across frames (the Annexe H confirmation).
    settings = _semantic_settings()
    return FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(
                transcript="bonjour le monde entier mes amis", samples_per_word=480
            ),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
    )


async def test_pushed_thinker_complete_fires_endpoint_without_poll() -> None:
    """A pass-conclusion push alone arms the semantic endpoint (issue 0120).

    The Thinker's ``user_turn_complete`` lands BETWEEN frames via
    ``note_thinker_complete`` (no per-frame ``thinker_complete`` poll, no
    inference-cadence debounce); the advancing stable prefix confirms it
    (Annexe H) and ONE silence frame fires the endpoint — far before the
    100-frame silence floor, the only other way this turn could end.
    """

    say = _RecordingSayPath(produce_audio=True)
    loop = _push_loop(say)
    assert await loop.start() is True

    # Voiced frames open the turn; the fake STT reveals ~1 word per frame.
    for _ in range(3):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING

    # A Thinker pass concludes BETWEEN frames → the bit is pushed immediately.
    loop.note_thinker_complete(True)

    # Two more voiced frames advance the stable prefix past the arm-time
    # watermark (the anti-false-positive confirmation) ...
    for _ in range(2):
        await loop.feed_raw_frame(_LOUD)
    # ... and a single silence frame fires the CONFIRMED semantic endpoint.
    await loop.feed_raw_frame(_QUIET)
    await loop.join()

    assert say.transcripts == ["bonjour le monde entier mes amis"]
    tos = [t["to"] for t in _turn_states()]
    assert "thinking" in tos
    latencies = _latency_events()
    assert latencies and "t_endpoint" in latencies[-1]["marks"]


async def test_pushed_bit_outside_user_speaking_is_dropped() -> None:
    """A push while the user does not hold the floor must not arm anything.

    A late pass can outlive its turn (the push fires after ``endpoint`` /
    before any speech); it must never pre-arm the NEXT turn's endpoint.
    """

    say = _RecordingSayPath(produce_audio=True)
    loop = _push_loop(say)
    await loop.start()

    loop.note_thinker_complete(True)  # idle — dropped

    for _ in range(5):
        await loop.feed_raw_frame(_LOUD)
    # The stable prefix advanced plenty, but nothing was armed: a short trailing
    # silence (far under the 100-frame floor) must NOT end the turn early.
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    assert say.transcripts == []
    assert loop.state is TurnState.USER_SPEAKING
    await loop.stop()


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
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
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
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        await on_first_audio()  # → bob_speaking
        if on_audio_chunk is not None:
            await on_audio_chunk(b"\x00\x00\x00\x00", 24_000)
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


# --- persistence hook at finalize (PRD 0016 / issue 0109) --------------------


class _RecordingPersist:
    """Captures every :class:`PersistedTurn` the loop hands to ``persist_turn``."""

    def __init__(self) -> None:
        self.turns: list[PersistedTurn] = []

    async def __call__(self, turn: PersistedTurn) -> None:
        self.turns.append(turn)


async def test_completed_turn_persists_with_audio_and_transcript() -> None:
    persist = _RecordingPersist()
    say = _RecordingSayPath(produce_audio=True, audio_chunk_bytes=64)
    loop = _loop(say, transcript="bonjour le monde", persist_turn=persist)
    await loop.start()

    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    assert len(persist.turns) == 1
    snap = persist.turns[0]
    assert snap.end_reason == "completed"
    assert snap.turn_id == say.turn_ids[0]
    assert snap.final_transcript == "bonjour le monde"
    # The user's voiced frames were captured as mic_in PCM…
    assert len(snap.mic_pcm) > 0
    assert snap.mic_sample_rate == 16_000
    # …and Bob's outbound chunk as tts_out PCM at the TTS rate.
    assert snap.tts_pcm == b"\x00" * 64
    assert snap.tts_sample_rate == 24_000
    # The FULL Annexe F latency struct rode along into the persistence snapshot
    # (issue 0110): the 0100 basics + the formalised t_first_partial / t_tts_end,
    # and the derived endpoint_to_first_audio_ms computed from the marks.
    assert "t_first_mic_frame" in snap.marks
    assert "t_first_partial" in snap.marks
    assert "t_endpoint" in snap.marks
    assert "t_first_audio_chunk" in snap.marks
    assert "t_tts_end" in snap.marks
    assert "endpoint_to_first_audio_ms" in snap.derived
    # The feature-gated derived carry their not-wired defaults (stable schema).
    assert snap.derived["backchannel_ms"] is None
    assert snap.derived["draft_hit"] is False


async def test_voice_stop_midturn_persists_voice_stop() -> None:
    persist = _RecordingPersist()
    say = _RecordingSayPath()
    loop = _loop(say, persist_turn=persist)
    await loop.start()
    # Only a voiced burst — the user is still speaking (no endpoint yet).
    for _ in range(5):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING

    await loop.stop()

    assert len(persist.turns) == 1
    assert persist.turns[0].end_reason == "voice_stop"
    # The in-flight utterance was captured.
    assert len(persist.turns[0].mic_pcm) > 0


async def test_stop_when_idle_persists_nothing() -> None:
    """A ``voice_stop`` between turns (FSM idle) writes no turn."""

    persist = _RecordingPersist()
    say = _RecordingSayPath()
    loop = _loop(say, persist_turn=persist)
    await loop.start()
    # No frames → the FSM never left idle.
    assert loop.state is TurnState.IDLE

    await loop.stop()

    assert persist.turns == []


async def test_completed_then_stop_persists_once() -> None:
    """A completed turn followed by socket-close persists exactly once.

    The completed turn persists on ``_finalize_say``; the trailing ``stop`` finds
    the FSM idle (turn already done) and the id already persisted, so the
    idempotency guard makes it a no-op — no duplicate row / blobs.
    """

    persist = _RecordingPersist()
    say = _RecordingSayPath(produce_audio=True, audio_chunk_bytes=16)
    loop = _loop(say, transcript="bonjour", persist_turn=persist)
    await loop.start()
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()
    await loop.stop()

    assert len(persist.turns) == 1
    assert persist.turns[0].end_reason == "completed"


async def test_persist_hook_failure_does_not_break_loop() -> None:
    """A raising ``persist_turn`` is swallowed — the turn still completes."""

    async def _boom(turn: PersistedTurn) -> None:
        raise RuntimeError("disk full")

    say = _RecordingSayPath(produce_audio=True)
    loop = _loop(say, transcript="bonjour", persist_turn=_boom)
    await loop.start()
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    # The loop reached idle despite the hook raising.
    assert loop.state is TurnState.IDLE


async def test_bargein_persists_with_committed_spoken_text() -> None:
    persist = _RecordingPersist()
    say = _SpeakingSayPath(played="Bonjour le monde")
    settings = Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=120,
        STT_DEBUG_TEXT_MAX_CHARS=64,
        BARGEIN_CONFIRM_MS=90,
    )
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        persist_turn=persist,
    )
    loop._now = _fake_clock(step_ms=30.0)  # type: ignore[method-assign]
    await _drive_to_bob_speaking(loop, say)

    # Continuous voiced burst past the 90 ms window → confirmed barge-in.
    for _ in range(5):
        await loop.feed_raw_frame(_LOUD)

    say.release.set()
    await loop.stop()

    # Exactly one persist — the barge-in cut — carrying the played prefix.
    bargein_turns = [t for t in persist.turns if t.end_reason == "bargein"]
    assert len(bargein_turns) == 1
    assert bargein_turns[0].spoken_text == "Bonjour le monde"
    # The trailing stop did NOT add a second persist for the same turn id.
    assert len(persist.turns) == 1


# --- backchannels (PRD 0016 / issue 0105, Annexe B + A.2 + F) ----------------


class _RecordingBackchannelTts:
    """A fake backchannel TTS hook: records (turn_id, token) it was asked to play."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, turn_id: str, token: str) -> None:
        self.calls.append((turn_id, token))


async def _open_turn(loop: FullDuplexLoop, *, loud_frames: int = 6) -> None:
    """Drive the loop into ``user_speaking`` with a voiced burst (feeds STT)."""

    await loop.start()
    for _ in range(loud_frames):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING


async def test_pause_with_trigger_emits_backchannel_without_floor_change() -> None:
    say = _RecordingSayPath(produce_audio=True)
    tts = _RecordingBackchannelTts()
    loop = _loop(
        say,
        transcript="bonjour le monde",
        backchannel_trigger=lambda: "mm",
        backchannel_tts=tts,
    )
    await _open_turn(loop)

    # A short mid-utterance pause (2 quiet frames = VAD_PAUSE_MS) trips vad_pause
    # but NOT the endpoint (4 frames). The backchannel fires in that pause.
    for _ in range(2):
        await loop.feed_raw_frame(_QUIET)
    # The synthesis is fire-and-forget (issue 0120): yield once so the spawned
    # task runs the (instant) fake TTS.
    await asyncio.sleep(0)

    # The acknowledgement was synthesised + the event emitted...
    assert tts.calls and tts.calls[0][1] == "mm"
    backchannels = _backchannel_events()
    assert len(backchannels) == 1
    assert backchannels[0]["token"] == "mm"
    # ...WITHOUT a floor transition: the FSM is still in user_speaking (Bob never
    # took the floor for the backchannel — no bob_speaking from it).
    assert loop.state is TurnState.USER_SPEAKING
    tos = [t["to"] for t in _turn_states(loop._fsm.turn_id)]
    assert "bob_speaking" not in tos

    await loop.stop()


async def test_pause_without_trigger_emits_no_backchannel() -> None:
    say = _RecordingSayPath()
    tts = _RecordingBackchannelTts()
    # The Thinker has nothing to interject (no trigger).
    loop = _loop(say, backchannel_trigger=lambda: None, backchannel_tts=tts)
    await _open_turn(loop)
    for _ in range(2):
        await loop.feed_raw_frame(_QUIET)

    # A pause the Thinker did not flag stays silent (the gate is not systematic).
    assert tts.calls == []
    assert _backchannel_events() == []
    await loop.stop()


async def test_continuous_speech_emits_no_backchannel_over_active_speech() -> None:
    say = _RecordingSayPath()
    tts = _RecordingBackchannelTts()
    loop = _loop(say, backchannel_trigger=lambda: "mm", backchannel_tts=tts)
    await loop.start()
    # Continuous voiced frames: no pause → no vad_pause → no backchannel over the
    # user's active speech, even though the Thinker carries a trigger.
    for _ in range(10):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING
    assert tts.calls == []
    assert _backchannel_events() == []
    await loop.stop()


async def test_backchannel_stamps_backchannel_ms_in_turn_latency() -> None:
    say = _RecordingSayPath(produce_audio=True)
    loop = _loop(
        say,
        transcript="bonjour le monde",
        backchannel_trigger=lambda: "ok",
        backchannel_tts=_RecordingBackchannelTts(),
    )
    await _open_turn(loop)
    # Mid pause → backchannel, then resume + trailing silence → endpoint → reply.
    for _ in range(2):
        await loop.feed_raw_frame(_QUIET)
    for _ in range(4):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(6):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    # The turn_latency carries the backchannel marks + a real backchannel_ms.
    latencies = _latency_events()
    assert latencies, "expected a turn_latency event"
    marks = latencies[-1]["marks"]
    assert "t_backchannel_pause" in marks
    assert "t_backchannel" in marks
    derived = latencies[-1]["derived"]
    assert derived["backchannel_ms"] is not None
    assert derived["backchannel_ms"] >= 0


class _GatedBackchannelTts:
    """A SLOW fake backchannel TTS: parks on a gate until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls: list[tuple[str, str]] = []
        self.finished = 0

    async def __call__(self, turn_id: str, token: str) -> None:
        self.calls.append((turn_id, token))
        await self.release.wait()
        self.finished += 1


async def test_slow_backchannel_synthesis_does_not_block_frame_loop() -> None:
    """The frame loop keeps processing while a backchannel synthesises (0120).

    Before issue 0120 the loop AWAITED the synthesis inside the frame handler —
    a parked TTS froze the pump on the pause frame. Now the synthesis is
    fire-and-forget: the ``backchannel`` event fires at dispatch, subsequent
    frames are processed, and the whole turn completes normally while the TTS
    is STILL parked.
    """

    say = _RecordingSayPath(produce_audio=True)
    tts = _GatedBackchannelTts()
    # One-shot trigger so the later pre-endpoint pause does not dispatch a
    # second backchannel (the refractory is 0 in these settings).
    triggers = iter(["mm"])
    loop = _loop(
        say,
        transcript="bonjour le monde",
        backchannel_trigger=lambda: next(triggers, None),
        backchannel_tts=tts,
    )
    await _open_turn(loop)

    # The pause trips the backchannel; the synthesis parks on the gate.
    for _ in range(2):
        await loop.feed_raw_frame(_QUIET)
    await asyncio.sleep(0)  # let the spawned task reach the gate
    assert [token for _turn, token in tts.calls] == ["mm"]
    assert tts.finished == 0  # parked — and the frame handler already returned
    # The dispatch already emitted the event (not gated on synthesis end).
    assert len(_backchannel_events()) == 1

    # The frame loop is NOT blocked: the user resumes and finishes the turn
    # while the synthesis is still parked; the endpoint fires, the reply runs.
    for _ in range(4):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING
    for _ in range(6):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert say.transcripts == ["bonjour le monde"]
    assert tts.finished == 0  # the whole turn completed while the TTS was parked
    assert len(_backchannel_events()) == 1

    # Teardown cancels the parked fire-and-forget task (no leak).
    await loop.stop()


async def test_backchannel_synthesis_failure_never_affects_the_turn() -> None:
    """An exception in the fire-and-forget synthesis is contained (issue 0120).

    The local supervisor logs it (never re-raises into the frame loop); the
    user keeps the floor and the turn runs to a normal completion exactly as if
    the backchannel had succeeded.
    """

    async def _boom(turn_id: str, token: str) -> None:
        raise RuntimeError("kokoro down")

    say = _RecordingSayPath(produce_audio=True)
    loop = _loop(
        say,
        transcript="bonjour le monde",
        backchannel_trigger=lambda: "mm",
        backchannel_tts=_boom,
    )
    await _open_turn(loop)
    for _ in range(2):
        await loop.feed_raw_frame(_QUIET)
    await asyncio.sleep(0)  # the spawned synthesis raises (and is supervised)

    # The failure never touched the turn: the user still holds the floor...
    assert loop.state is TurnState.USER_SPEAKING
    # ...and the turn runs to a normal completion (endpoint → reply).
    for _ in range(4):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(6):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert say.transcripts == ["bonjour le monde"]


async def test_no_backchannel_hooks_is_inert() -> None:
    # The bare loop (no Thinker / no backchannel hooks) never fires a backchannel
    # even across pauses — zero regression for the 0100/0101 behaviour.
    say = _RecordingSayPath(produce_audio=True)
    loop = _loop(say, transcript="bonjour le monde")
    await _open_turn(loop)
    for _ in range(2):
        await loop.feed_raw_frame(_QUIET)
    assert _backchannel_events() == []
    await loop.stop()


async def test_backchannel_tts_failure_does_not_break_turn() -> None:
    say = _RecordingSayPath(produce_audio=True)

    async def _boom(turn_id: str, token: str) -> None:
        raise RuntimeError("tts down")

    loop = _loop(say, backchannel_trigger=lambda: "mm", backchannel_tts=_boom)
    await _open_turn(loop)
    for _ in range(2):
        await loop.feed_raw_frame(_QUIET)
    # The synthesis raised but was swallowed — the turn is intact (still
    # user_speaking) and the event still fired (the gate decided to emit).
    assert loop.state is TurnState.USER_SPEAKING
    assert len(_backchannel_events()) == 1
    await loop.stop()


# --- speculative draft wiring (PRD 0016 / issue 0104) ------------------------


class _FakeDraftClientLoop:
    """A trivial ``draft`` client: echoes a fixed reply for any partial."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[str] = []

    def supports_guided_json(self) -> bool:
        return False

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls.append(next((m["content"] for m in reversed(messages)), ""))
        return self._reply

    async def complete(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def _draft_status_events() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "draft_status":
            out.append(ws_event)
    return out


def _loop_with_draft(
    say_path: _RecordingSayPath,
    *,
    draft_reply: str,
    transcript: str = "quel temps fait il",
    revise_to: str | None = None,
) -> tuple[FullDuplexLoop, SpeculativeDraft]:
    """Build a loop with a REAL SpeculativeDraft wired behind the draft hooks.

    ``revise_to`` (issue 0104): when set, the fake STT streams ``transcript`` as
    partials (what the draft fires on) but freezes to this divergent final — so
    the commit gate sees a non-prefix, low-overlap final and discards.
    """

    settings = _settings()
    # Pin the debounce to 0 so the draft fires on the first partial of the short
    # synthetic turn (mirrors the attest ``draft: true`` knob).
    settings = settings.model_copy(
        update={"THINKER_DEBOUNCE_MS": 0, "DRAFT_COMMIT_SIMILARITY": 0.6}
    )
    drafter = SpeculativeDraft(
        client=_FakeDraftClientLoop(draft_reply),  # type: ignore[arg-type]
        settings=settings,
        session_id="s1",
        spawn=asyncio.create_task,
    )
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript=transcript, samples_per_word=160, revise_to=revise_to),
            session_id="s1",
            settings=settings,
        ),
        say_path=say_path,
        settings=settings,
        session_id="s1",
        on_draft_start=drafter.start,
        on_draft_feed=drafter.feed_partial,
        on_draft_stop=drafter.stop,
        draft_commit_gate=drafter.commit_gate,
        draft_emit_decision=drafter.emit_decision,
    )
    return loop, drafter


async def test_committed_draft_is_adopted_into_say_path() -> None:
    say = _RecordingSayPath(produce_audio=True)
    loop, _drafter = _loop_with_draft(say, draft_reply="Il fait beau à Paris.")
    await loop.start()
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    # The final transcript == the partial the draft fired on → prefix fast-path
    # commit. The committed text is re-injected into the say-path verbatim.
    assert say.prepared_replies == ["Il fait beau à Paris."]
    # draft_status reached committed; the latency body flips draft_hit + carries
    # the draft marks.
    states = [e["state"] for e in _draft_status_events()]
    assert states[-1] == "committed"
    latency = _latency_events()[-1]
    assert latency["derived"]["draft_hit"] is True
    assert "t_draft_ready" in latency["marks"]
    assert "t_commit_decision" in latency["marks"]


async def test_divergent_final_discards_draft_and_runs_cold() -> None:
    say = _RecordingSayPath(produce_audio=True)
    # The draft fires on the streamed "reserve une table..." partials, but the
    # final REVISES to a divergent clause → the gate discards → cold say-path.
    loop, _drafter = _loop_with_draft(
        say,
        draft_reply="Je réserve une table.",
        transcript="reserve une table pour ce soir",
        revise_to="annule tout finalement laisse tomber",
    )
    await loop.start()
    for _ in range(10):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    # COLD path: no prepared reply, the say-path ran on the (divergent) final
    # transcript itself.
    assert say.prepared_replies == [None]
    assert say.transcripts == ["annule tout finalement laisse tomber"]
    states = [e["state"] for e in _draft_status_events()]
    assert states[-1] == "discarded"
    latency = _latency_events()[-1]
    assert latency["derived"]["draft_hit"] is False


async def test_no_drafter_keeps_cold_path_unchanged() -> None:
    # The bare loop (no draft hooks) — every turn is COLD, prepared_reply is None,
    # and draft_hit stays False (Annexe G degradation shape).
    say = _RecordingSayPath(produce_audio=True)
    loop = _loop(say, transcript="bonjour")
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    assert say.prepared_replies == [None]
    assert not _draft_status_events()
    latency = _latency_events()[-1]
    assert latency["derived"]["draft_hit"] is False
    assert "t_draft_ready" not in latency["marks"]
    assert "t_commit_decision" not in latency["marks"]


# --- turn metrics (PRD 0018 / issue 0117) ------------------------------------


def _turn_metrics_events() -> list[dict[str, Any]]:
    """The ``turn_metrics`` ws_event bodies currently in the debug ring buffer."""

    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "turn_metrics":
            out.append(ws_event)
    return out


class _MarkingSayPath(_RecordingSayPath):
    """A say-path that stamps the downstream marks like the real driver does.

    The real say-path sites (orchestrator first LLM token, ws_router TTS
    first-chunk/first-byte) resolve the turn through the metrics ContextVar the
    loop binds for the say task — this fake exercises that exact seam.
    """

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        turn_metrics.mark_current("llm_first_token")
        turn_metrics.mark_current("tts_first_chunk")
        turn_metrics.count_current("validation_retry")
        await super().__call__(
            transcript,
            turn_id=turn_id,
            on_first_audio=on_first_audio,
            on_spoken_progress=on_spoken_progress,
            on_audio_chunk=on_audio_chunk,
            prepared_reply=prepared_reply,
        )
        turn_metrics.mark_current("audio_first_byte")


async def test_completed_turn_emits_turn_metrics_summary() -> None:
    say = _MarkingSayPath(produce_audio=True)
    loop = _loop(say, transcript="bonjour le monde")
    await loop.start()
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    events = _turn_metrics_events()
    assert len(events) == 1
    body = events[0]
    assert body["turn_id"] == say.turn_ids[0]

    # The loop-owned endpoint-path stages are all timed...
    stages = body["stages_ms"]
    for stage in ("endpoint", "loops_frozen", "stt_finalized", "gate_decided"):
        assert stage in stages
    # ...and the say-path stages resolved through the bound ContextVar.
    for stage in ("llm_first_token", "tts_first_chunk", "audio_first_byte"):
        assert stage in stages
    # The marks are chronological: each stage duration is >= 0 and the marks
    # follow the pipeline order endpoint -> ... -> audio_first_byte.
    assert all(duration >= 0 for duration in stages.values())
    marks = body["marks"]
    assert marks["endpoint"] <= marks["stt_finalized"] <= marks["gate_decided"]
    assert marks["gate_decided"] <= marks["llm_first_token"] <= marks["audio_first_byte"]

    # Counters carry the stable schema + the retry the say-path counted.
    counters = body["counters"]
    assert counters["validation_retry"] == 1
    assert counters["draft_adopted"] == 0
    assert counters["draft_discarded"] == 0

    # The rolling aggregates ride the same event (consultable in the Debug
    # View) and already include this turn's samples.
    aggregates = body["aggregates"]
    assert aggregates["turns_measured"] == 1
    assert aggregates["stages"]["endpoint"]["count"] == 1
    assert "p50_ms" in aggregates["stages"]["endpoint"]
    assert "p95_ms" in aggregates["stages"]["endpoint"]


async def test_committed_draft_turn_counts_draft_adopted() -> None:
    say = _RecordingSayPath(produce_audio=True)
    loop, _drafter = _loop_with_draft(say, draft_reply="Il fait beau à Paris.")
    await loop.start()
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    body = _turn_metrics_events()[-1]
    assert body["counters"]["draft_adopted"] == 1
    assert body["counters"]["draft_discarded"] == 0
    assert body["aggregates"]["draft_adoption_rate"] == 1.0


async def test_discarded_draft_turn_counts_draft_discarded() -> None:
    say = _RecordingSayPath(produce_audio=True)
    loop, _drafter = _loop_with_draft(
        say,
        draft_reply="Je réserve une table.",
        transcript="reserve une table pour ce soir",
        revise_to="annule tout finalement laisse tomber",
    )
    await loop.start()
    for _ in range(10):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    body = _turn_metrics_events()[-1]
    assert body["counters"]["draft_adopted"] == 0
    assert body["counters"]["draft_discarded"] == 1
    assert body["aggregates"]["draft_adoption_rate"] == 0.0


async def test_voice_stop_midturn_still_emits_turn_metrics() -> None:
    say = _RecordingSayPath()
    loop = _loop(say)
    await loop.start()
    for _ in range(5):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING

    await loop.stop()
    # The turn never reached the endpoint, but the teardown still closes its
    # metrics entry with a (stage-less) summary — no silent leak.
    events = _turn_metrics_events()
    assert len(events) == 1
    assert events[0]["stages_ms"] == {}


async def test_one_metrics_summary_per_turn_across_consecutive_turns() -> None:
    say = _RecordingSayPath(produce_audio=True)
    loop = _loop(say, transcript="bonjour")
    await loop.start()
    for _ in range(2):
        for _ in range(6):
            await loop.feed_raw_frame(_LOUD)
        for _ in range(8):
            await loop.feed_raw_frame(_QUIET)
        await loop.join()

    events = _turn_metrics_events()
    assert len(events) == 2
    # Two distinct turns, each summarised exactly once.
    assert len({e["turn_id"] for e in events}) == 2
    body = events[-1]
    assert body["aggregates"]["turns_measured"] == 2
    assert body["aggregates"]["stages"]["endpoint"]["count"] == 2


async def test_persist_hook_failure_emits_voice_persist_failed() -> None:
    """A lost voice turn is client-visible (issue 0124), not a log-only swallow.

    The event must reach BOTH sinks: the debug ring buffer (``/ws/debug``) and
    the registered chat WS emitter (the client).
    """

    forwarded: list[dict[str, Any]] = []

    async def _collect(event: dict[str, Any]) -> None:
        forwarded.append(event)

    async def _boom(turn: PersistedTurn) -> None:
        raise RuntimeError("disk full")

    event_bus_v2.set_ws_emitter(_collect)
    try:
        say = _RecordingSayPath(produce_audio=True)
        loop = _loop(say, transcript="bonjour", persist_turn=_boom)
        await loop.start()
        for _ in range(8):
            await loop.feed_raw_frame(_LOUD)
        for _ in range(8):
            await loop.feed_raw_frame(_QUIET)
        await loop.join()
    finally:
        event_bus_v2.set_ws_emitter(None)

    # The loop survived (existing contract) AND the failure was surfaced.
    assert loop.state is TurnState.IDLE
    failed = [e for e in forwarded if e.get("type") == "voice_persist_failed"]
    assert len(failed) == 1
    assert failed[0]["turn_id"] == say.turn_ids[0]
    assert failed[0]["end_reason"] == "completed"
    assert failed[0]["error"] == "RuntimeError: disk full"

    # Same body in the debug ring buffer (the /ws/debug surface).
    buffered = [
        (event.payload or {}).get("ws_event") or {}
        for event in debug_log.snapshot()
        if ((event.payload or {}).get("ws_event") or {}).get("type") == "voice_persist_failed"
    ]
    assert len(buffered) == 1
    assert buffered[0]["turn_id"] == say.turn_ids[0]


# --- endpoint concurrency (PRD 0018 / issue 0118) -----------------------------


def _stt_final_events() -> list[dict[str, Any]]:
    """The ``stt_final`` ws_event bodies currently in the debug ring buffer."""

    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "stt_final":
            out.append(ws_event)
    return out


async def _until(predicate: Callable[[], bool], *, what: str) -> None:
    """Poll ``predicate`` until true (bounded) — awaits a parallel branch."""

    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"condition never reached: {what}")


class _ParkedStopHook:
    """A freeze hook that records entry and parks until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    async def __call__(self) -> None:
        self.entered.set()
        await self.release.wait()
        self.finished = True


class _ParkedRoleClient:
    """A role client whose pass parks forever — the cooperative cancel never
    unhooks it (only the post-cap hard :meth:`asyncio.Task.cancel` does)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._gate = asyncio.Event()  # never set

    def supports_guided_json(self) -> bool:
        return False

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.started.set()
        await self._gate.wait()
        return ""

    async def complete(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


async def test_endpoint_freezes_thinker_and_draft_concurrently() -> None:
    """Both freezes start at the same instant; STT finalizes in parallel (0118).

    Under the pre-0118 sequential path the Draft stop could only START after
    the Thinker stop RETURNED, and the STT finalize only after both: with both
    hooks parked, the draft hook would never be entered and no ``stt_final``
    could exist. Here both hooks are entered while BOTH are still parked
    (concurrent fan-out), and the full-buffer STT pass has already frozen the
    final transcript while the freezes are still in flight.
    """

    say = _RecordingSayPath(produce_audio=True)
    thinker_stop = _ParkedStopHook()
    draft_stop = _ParkedStopHook()
    settings = _settings()
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour le monde", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        on_thinker_stop=thinker_stop,
        on_draft_stop=draft_stop,
    )
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)

    # Drive the trailing silence from a background task: the endpoint frame
    # parks inside the (gated) freeze fan-out, so it must not block the test.
    async def _silence() -> None:
        for _ in range(8):
            await loop.feed_raw_frame(_QUIET)

    pump = asyncio.create_task(_silence())
    try:
        # CONCURRENT fan-out: both freezes are entered while NEITHER finished.
        await asyncio.wait_for(thinker_stop.entered.wait(), timeout=1.0)
        await asyncio.wait_for(draft_stop.entered.wait(), timeout=1.0)
        assert thinker_stop.finished is False
        assert draft_stop.finished is False

        # The STT finalization did NOT wait for the freezes: the final
        # transcript is already frozen (``stt_final`` emitted) while both
        # stops are still parked.
        await _until(lambda: bool(_stt_final_events()), what="stt_final during freeze")
        assert _stt_final_events()[-1]["text"] == "bonjour le monde"

        # The say-path waits for the commit-gate decision (which follows the
        # capped freeze) — it has not launched yet.
        assert say.transcripts == []
    finally:
        thinker_stop.release.set()
        draft_stop.release.set()
        await pump
    await loop.join()

    # Released freezes → gate → say-path on the (already) finalized transcript.
    assert say.transcripts == ["bonjour le monde"]
    assert loop.state is TurnState.IDLE


async def test_stalled_anticipation_is_hard_cancelled_within_grace_cap() -> None:
    """Acceptance (0118): stalling fakes never hold the say-path past the cap.

    A REAL ThinkerLoop + REAL SpeculativeDraft run with clients whose in-flight
    pass parks forever (the cooperative cancel never unhooks it) under a 2 s
    configured grace. The 40 ms cap hard-cancels both — CONCURRENTLY — so the
    endpoint frame returns (say-path spawned) within the cap window, nowhere
    near the 2 s + 2 s the sequential uncapped graces would have cost.
    """

    settings = _settings().model_copy(
        update={
            "THINKER_DEBOUNCE_MS": 0,
            "THINKER_CANCEL_GRACE_MS": 2_000,
            "THINKER_CANCEL_GRACE_CAP_MS": 40,
            "DRAFT_COMMIT_SIMILARITY": 0.6,
        }
    )
    thinker_client = _ParkedRoleClient()
    draft_client = _ParkedRoleClient()
    thinker = ThinkerLoop(
        client=thinker_client,  # type: ignore[arg-type]
        live_state=LiveTranscriptState(),
        settings=settings,
        session_id="s1",
        spawn=asyncio.create_task,
    )
    drafter = SpeculativeDraft(
        client=draft_client,  # type: ignore[arg-type]
        settings=settings,
        session_id="s1",
        spawn=asyncio.create_task,
    )
    say = _RecordingSayPath(produce_audio=True)
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour le monde", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        on_thinker_start=thinker.start,
        on_thinker_feed=thinker.feed_partial,
        on_thinker_stop=thinker.stop,
        on_draft_start=drafter.start,
        on_draft_feed=drafter.feed_partial,
        on_draft_stop=drafter.stop,
        draft_commit_gate=drafter.commit_gate,
        draft_emit_decision=drafter.emit_decision,
    )
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    # Both anticipation passes are genuinely IN FLIGHT (parked on their gates).
    await asyncio.wait_for(thinker_client.started.wait(), timeout=1.0)
    await asyncio.wait_for(draft_client.started.wait(), timeout=1.0)
    assert thinker.inflight is True
    assert drafter.inflight is True

    started = asyncio.get_running_loop().time()
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    elapsed = asyncio.get_running_loop().time() - started
    await loop.join()

    # The endpoint path (freeze fan-out + finalize + gate + say spawn) fit in
    # the cap window + epsilon — NOT the 4 s of sequential uncapped graces.
    assert elapsed < 1.0, f"endpoint path took {elapsed:.3f}s — the grace cap did not apply"
    assert say.transcripts == ["bonjour le monde"]
    assert say.prepared_replies == [None]  # no draft ever landed → cold path
    assert thinker.inflight is False
    assert drafter.inflight is False

    # The 0117 summary decomposes the concurrent endpoint stages.
    body = _turn_metrics_events()[-1]
    stages = body["stages_ms"]
    for stage in ("endpoint", "loops_frozen", "stt_finalized", "gate_decided"):
        assert stage in stages
    # ``loops_frozen`` reflects the capped (40 ms) freeze, not the 2 s grace.
    marks = body["marks"]
    assert marks["loops_frozen"] - marks["endpoint"] < 1.5


async def test_endpoint_without_inflight_pass_behaves_as_before() -> None:
    """No-regression (0118): an idle freeze + instant finalize = the old path.

    Real Thinker + Draft loops with INSTANT clients whose passes are long done
    by the endpoint: the fan-out has nothing to cancel, the commit gate adopts
    the landed draft, and the turn walks the exact 0100/0104 cycle.
    """

    say = _RecordingSayPath(produce_audio=True)
    loop, drafter = _loop_with_draft(say, draft_reply="Il fait beau à Paris.")
    await loop.start()
    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    # Let the (instant) draft pass land before the endpoint silence.
    await drafter.join()
    assert drafter.inflight is False
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    # The committed draft is still adopted into the say-path verbatim.
    assert say.transcripts == ["quel temps fait il"]
    assert say.prepared_replies == ["Il fait beau à Paris."]
    assert loop.state is TurnState.IDLE
    body = _turn_metrics_events()[-1]
    for stage in ("endpoint", "loops_frozen", "stt_finalized", "gate_decided"):
        assert stage in body["stages_ms"]


# --- barge-in zero-grace (PRD 0018 / issue 0119) ------------------------------


class _StubbornRoleClient:
    """A role client whose in-flight pass SWALLOWS the cooperative cancel.

    Worse than :class:`_ParkedRoleClient`: even the post-grace hard
    ``Task.cancel`` of the 0118 ladder would stall in its final await, because
    the coroutine traps ``CancelledError`` and keeps waiting. Only the 0119
    zero-grace ``hard_cancel`` (which never awaits the task) cuts past it.
    ``escape`` unblocks the parked pass at test teardown.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.escape = asyncio.Event()

    def supports_guided_json(self) -> bool:
        return False

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.started.set()
        while not self.escape.is_set():
            try:
                await self.escape.wait()
            except asyncio.CancelledError:
                continue  # the cooperative cancel stalls — by design
        return "{}"

    async def complete(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


class _PipelineSayPath:
    """A say-path that streams through a REAL :class:`SpeechStreamPipeline`.

    Mimics ws_router's ``_synthesize_and_stream`` (issue 0121): an endless fake
    synthesizer keeps producing chunks, the sink stamps each accepted chunk
    with the loop's fake clock, and the in-flight pipeline is exposed so the
    test wires the loop's ``cancel_speech`` hook to its single ``cancel()`` —
    the 0119 barge-in surface.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.pipeline: SpeechStreamPipeline | None = None
        self.first_chunk_sent = asyncio.Event()
        self.sent_at: list[float] = []
        self.unwound = False

    def cancel_speech(self) -> None:
        """The loop's ``cancel_speech`` hook — the pipeline's ONE kill-switch."""

        if self.pipeline is not None:
            self.pipeline.cancel()

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        async def _synthesize(sentence: str) -> AsyncIterator[SynthesisChunk]:
            for _ in range(100_000):  # effectively endless — only the cut stops it
                yield SynthesisChunk(pcm16=b"\x00\x00" * 8, sample_rate=24_000)

        first = True

        async def _send(chunk: SynthesisChunk) -> None:
            nonlocal first
            if first:
                first = False
                await on_first_audio()
            self.sent_at.append(self._clock())
            self.first_chunk_sent.set()

        self.pipeline = SpeechStreamPipeline(synthesize=_synthesize, send_chunk=_send)
        try:
            await self.pipeline.run(["une phrase sans fin"])
        except asyncio.CancelledError:
            self.unwound = True
            raise


async def test_bargein_cuts_tts_pipeline_with_no_audio_after_cut() -> None:
    """Acceptance (0119): after the confirmation, NO audio chunk leaves.

    Real :class:`SpeechStreamPipeline` + fake TTS + the deterministic fake
    clock: every chunk the sink accepts is stamped on the SAME strictly
    increasing clock that stamps ``t_cut``, so "no audio after the cut" is
    exact (stronger than the 300 ms budget — zero chunks past ``cut_ts``).
    The cut happens via the loop's synchronous ``cancel_speech`` hook (the
    pipeline's single ``cancel()``), before any await of the say task unwind.
    """

    clock = _fake_clock(step_ms=30.0)
    say = _PipelineSayPath(clock)
    settings = Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=120,
        STT_DEBUG_TEXT_MAX_CHARS=64,
        BARGEIN_CONFIRM_MS=90,
    )
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        cancel_speech=say.cancel_speech,
    )
    loop._now = clock  # type: ignore[method-assign]
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await asyncio.wait_for(say.first_chunk_sent.wait(), timeout=2.0)
    assert loop.state is TurnState.BOB_SPEAKING
    turn_id = loop._fsm.turn_id

    # Continuous user speech past the 90 ms window confirms the barge-in.
    async def _interrupt() -> None:
        for _ in range(6):
            await loop.feed_raw_frame(_LOUD)

    await asyncio.wait_for(_interrupt(), timeout=2.0)

    assert loop.state.value == "user_speaking"
    pipeline = say.pipeline
    assert pipeline is not None
    assert pipeline.cancelled is True
    assert say.unwound is True  # the say task observed the cut and unwound

    # The wire event carries the cut instant; nothing was sent past it.
    bargeins = _bargein_events(turn_id)
    assert len(bargeins) == 1
    cut_ts = bargeins[0]["cut_ts"]
    assert say.sent_at, "the say-path streamed before the cut"
    assert max(say.sent_at) <= cut_ts, "an audio chunk left AFTER the barge-in cut"

    # No further chunk ever lands (the sink count is frozen at the cut).
    sent_at_cut = len(say.sent_at)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(say.sent_at) == sent_at_cut

    await loop.stop()


async def test_bargein_hard_cancels_anticipation_with_no_grace() -> None:
    """Acceptance (0119): the Thinker/Draft passes are cut with ZERO grace.

    REAL ThinkerLoop + SpeculativeDraft with STUBBORN clients (their passes
    swallow the cooperative cancel — even the 0118 capped ladder would hang in
    its final await). No stop hooks are wired, so both passes are still in
    flight while Bob speaks; the confirmed barge-in hard-cancels them via the
    synchronous ``on_thinker_cancel`` / ``on_draft_cancel`` hooks: both loops
    report idle immediately, while the stubborn tasks themselves are STILL
    parked — proof the cut never waited on their unwind.
    """

    settings = _settings().model_copy(
        update={
            "THINKER_DEBOUNCE_MS": 0,
            "THINKER_CANCEL_GRACE_MS": 2_000,
            "THINKER_CANCEL_GRACE_CAP_MS": 250,
            "BARGEIN_CONFIRM_MS": 90,
            "DRAFT_COMMIT_SIMILARITY": 0.6,
        }
    )
    thinker_client = _StubbornRoleClient()
    draft_client = _StubbornRoleClient()
    tasks: list[asyncio.Task[None]] = []

    def _spawn(coro: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        tasks.append(task)
        return task

    thinker = ThinkerLoop(
        client=thinker_client,  # type: ignore[arg-type]
        live_state=LiveTranscriptState(),
        settings=settings,
        session_id="s1",
        spawn=_spawn,
    )
    drafter = SpeculativeDraft(
        client=draft_client,  # type: ignore[arg-type]
        settings=settings,
        session_id="s1",
        spawn=_spawn,
    )
    say = _SpeakingSayPath(played="Bonjour")
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        on_thinker_start=thinker.start,
        on_thinker_feed=thinker.feed_partial,
        on_thinker_cancel=thinker.hard_cancel,
        on_draft_start=drafter.start,
        on_draft_feed=drafter.feed_partial,
        on_draft_cancel=drafter.hard_cancel,
    )
    loop._now = _fake_clock(step_ms=30.0)  # type: ignore[method-assign]
    try:
        await loop.start()
        for _ in range(6):
            await loop.feed_raw_frame(_LOUD)
        # Both anticipation passes are genuinely IN FLIGHT (parked, stubborn).
        await asyncio.wait_for(thinker_client.started.wait(), timeout=1.0)
        await asyncio.wait_for(draft_client.started.wait(), timeout=1.0)
        # No stop hooks wired → the endpoint freeze leaves them in flight.
        for _ in range(8):
            await loop.feed_raw_frame(_QUIET)
        await asyncio.wait_for(say.entered.wait(), timeout=2.0)
        assert loop.state is TurnState.BOB_SPEAKING
        assert thinker.inflight is True
        assert drafter.inflight is True

        # The confirmed barge-in must return promptly — bounded so a regression
        # back to a grace/await ladder fails the test instead of hanging it.
        # Stop feeding at the cut: a frame fed AFTER the floor returned would
        # feed the RESUMED utterance to the re-armed drafter (a legitimate new
        # pass that would muddy the inflight assertions below).
        async def _interrupt() -> None:
            for _ in range(6):
                await loop.feed_raw_frame(_LOUD)
                if loop.state is not TurnState.BOB_SPEAKING:
                    return

        await asyncio.wait_for(_interrupt(), timeout=2.0)

        assert loop.state.value == "user_speaking"
        # Both loops were hard-cancelled with no grace…
        assert thinker.inflight is False
        assert drafter.inflight is False
        # …and the stubborn passes are STILL parked: the cut never awaited them.
        assert tasks and all(not task.done() for task in tasks)
    finally:
        say.release.set()
        thinker_client.escape.set()
        draft_client.escape.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await loop.stop()


async def test_user_resume_during_thinking_cuts_speech_pipeline_too() -> None:
    """Barge-in during THINKING (0119): the resume edge applies the same cut.

    The user resumes while the say-path is still generating (no audio yet —
    or a stream whose first chunk has not flipped the FSM): the loop invokes
    the synchronous ``cancel_speech`` kill-switch BEFORE awaiting the say task
    unwind, exactly like the confirmed barge-in path.
    """

    cut_calls: list[str] = []
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
        cancel_speech=lambda: cut_calls.append("cut"),
    )
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await asyncio.wait_for(say.entered.wait(), timeout=2.0)
    assert loop.state is TurnState.THINKING

    # The user resumes: the say-path is cut (pipeline kill-switch + task
    # cancel) and the floor returns to the user.
    await asyncio.wait_for(loop.feed_raw_frame(_LOUD), timeout=2.0)
    assert loop.state.value == "user_speaking"
    assert cut_calls == ["cut"]

    await loop.stop()


async def test_bargein_cut_lands_in_interrupted_turn_metrics_summary() -> None:
    """Acceptance (0119): the cut time appears in the 0117 summary.

    The interrupted turn's ``turn_metrics`` summary (emitted by the cancelled
    say-path's finalize) carries the ``bargein_cut`` stage; the RESUMED turn
    (same id, fresh origin — the 0101 contract) does not inherit it.
    """

    say = _SpeakingSayPath(played="Bonjour le monde")
    loop = _bargein_loop(say, confirm_ms=90)
    await _drive_to_bob_speaking(loop, say)
    turn_id = loop._fsm.turn_id
    assert turn_id is not None

    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)

    interrupted = [b for b in _turn_metrics_events() if b["turn_id"] == turn_id]
    assert len(interrupted) == 1
    body = interrupted[0]
    assert "bargein_cut" in body["marks"]
    assert "bargein_cut" in body["stages_ms"]
    assert body["marks"]["bargein_cut"] >= body["marks"]["endpoint"]

    # The resumed utterance re-uses the id with a FRESH metrics origin: its own
    # summary (the voice_stop teardown) carries no stale cut mark.
    say.release.set()
    await loop.stop()
    summaries = [b for b in _turn_metrics_events() if b["turn_id"] == turn_id]
    assert len(summaries) == 2
    assert "bargein_cut" not in summaries[-1]["marks"]


async def test_bargein_endpoint_policy_keeps_capped_grace_stops() -> None:
    """The two cancellation policies stay DISTINCT (0118 vs 0119).

    On the SAME loop: the endpoint path freezes the anticipation via the
    cooperative ``stop`` hooks (capped grace — issue 0118), and the barge-in
    path cuts via the zero-grace ``cancel`` hooks WITHOUT calling the stops
    again — each path keeps its own ladder.
    """

    stop_calls: list[str] = []
    cancel_calls: list[str] = []

    async def _thinker_stop() -> None:
        stop_calls.append("thinker")

    async def _draft_stop() -> None:
        stop_calls.append("draft")

    say = _SpeakingSayPath(played="Bonjour")
    settings = Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=120,
        STT_DEBUG_TEXT_MAX_CHARS=64,
        BARGEIN_CONFIRM_MS=90,
    )
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="bonjour", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        on_thinker_stop=_thinker_stop,
        on_draft_stop=_draft_stop,
        on_thinker_cancel=lambda: cancel_calls.append("thinker"),
        on_draft_cancel=lambda: cancel_calls.append("draft"),
    )
    loop._now = _fake_clock(step_ms=30.0)  # type: ignore[method-assign]
    await loop.start()
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await say.entered.wait()
    assert loop.state is TurnState.BOB_SPEAKING

    # The ENDPOINT froze via the cooperative stops; no hard cancel fired.
    assert sorted(stop_calls) == ["draft", "thinker"]
    assert cancel_calls == []

    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)

    # The BARGE-IN cut via the zero-grace cancels; the stops were not re-run.
    assert loop.state.value == "user_speaking"
    assert sorted(cancel_calls) == ["draft", "thinker"]
    assert sorted(stop_calls) == ["draft", "thinker"]

    say.release.set()
    await loop.stop()


# --- say-path exception force-reset (PRD 0018 / issue 0125) -------------------


def _events_of(event_type: str) -> list[dict[str, Any]]:
    """The ws_event bodies of ``event_type`` currently in the debug ring buffer."""

    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == event_type:
            out.append(ws_event)
    return out


class _FailingSayPath:
    """A say-path that raises at a configurable stage — but only on call 1.

    ``before_first_audio`` raises before ``on_first_audio`` (the FSM never
    leaves ``thinking``); ``mid_streaming`` raises right after it (the FSM is
    in ``bob_speaking``). Later calls succeed and behave like
    :class:`_RecordingSayPath` so the same loop can prove the NEXT utterance
    works after the failure.
    """

    def __init__(self, *, fail_stage: str) -> None:
        self.calls = 0
        self.transcripts: list[str] = []
        self.turn_ids: list[str] = []
        self._fail_stage = fail_stage

    async def __call__(
        self,
        transcript: str,
        *,
        turn_id: str,
        on_first_audio: Callable[[], Awaitable[None]],
        on_spoken_progress: Callable[[str], Awaitable[None]] | None = None,
        on_audio_chunk: Callable[[bytes, int], Awaitable[None]] | None = None,
        prepared_reply: str | None = None,
    ) -> None:
        self.calls += 1
        self.transcripts.append(transcript)
        self.turn_ids.append(turn_id)
        fail = self.calls == 1
        if fail and self._fail_stage == "before_first_audio":
            raise RuntimeError("llm exploded before any audio")
        await on_first_audio()
        if fail and self._fail_stage == "mid_streaming":
            raise RuntimeError("tts exploded mid-stream")
        if on_spoken_progress is not None:
            await on_spoken_progress(transcript)


def _loop_for(say_path: Any, transcript: str = "bonjour") -> FullDuplexLoop:
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


async def _drive_turn(loop: FullDuplexLoop) -> None:
    """One voiced burst + trailing silence (endpoint) + await the say-path."""

    for _ in range(8):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(8):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()


async def test_say_path_exception_before_first_audio_returns_to_idle() -> None:
    """Driver raises BEFORE the first audio chunk → idle + observable event."""

    say = _FailingSayPath(fail_stage="before_first_audio")
    loop = _loop_for(say)
    assert await loop.start() is True

    await _drive_turn(loop)

    assert loop.state is TurnState.IDLE
    failed = _events_of("say_path_failed")
    assert len(failed) == 1
    assert failed[0]["turn_id"] == say.turn_ids[0]
    assert "llm exploded" in failed[0]["error"]
    # The FSM never reached bob_speaking on the failed turn.
    assert "bob_speaking" not in [t["to"] for t in _turn_states(say.turn_ids[0])]
    # No hard reset was needed: the legal finalize edges recovered.
    assert _events_of("fsm_force_reset") == []

    # The same armed window still works: the NEXT utterance runs normally.
    await _drive_turn(loop)
    assert say.calls == 2
    assert loop.state is TurnState.IDLE
    await loop.stop()


async def test_say_path_exception_mid_streaming_returns_to_idle() -> None:
    """Driver raises while Bob is speaking → idle + observable event."""

    say = _FailingSayPath(fail_stage="mid_streaming")
    loop = _loop_for(say)
    assert await loop.start() is True

    await _drive_turn(loop)

    assert loop.state is TurnState.IDLE
    failed = _events_of("say_path_failed")
    assert len(failed) == 1
    assert "tts exploded" in failed[0]["error"]
    # The failed turn DID reach bob_speaking, and the finalize closed it.
    tos = [t["to"] for t in _turn_states(say.turn_ids[0])]
    assert "bob_speaking" in tos
    assert tos[-1] == "idle"

    # The same armed window still works afterwards.
    await _drive_turn(loop)
    assert say.calls == 2
    assert loop.state is TurnState.IDLE
    await loop.stop()


async def test_finalize_failure_hard_resets_fsm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even ``_finalize_say`` failing wholesale cannot wedge the FSM (issue 0125).

    The say-path completes (FSM in ``bob_speaking``) but the finalize itself
    blows up (e.g. the WS closed under it). The defensive force-reset must
    restore idle, emit the ``fsm_force_reset`` event, close the turn's 0117
    metrics entry (no collector leak) and re-arm STT so the next utterance
    works.
    """

    say = _RecordingSayPath(produce_audio=True)
    loop = _loop_for(say)
    assert await loop.start() is True

    real_finalize = loop._finalize_say
    armed = True

    async def _broken_finalize(turn_id: str) -> None:
        nonlocal armed
        if armed:
            armed = False
            raise RuntimeError("ws closed during finalize")
        await real_finalize(turn_id)

    monkeypatch.setattr(loop, "_finalize_say", _broken_finalize)
    await _drive_turn(loop)

    # The FSM was stuck in bob_speaking when finalize blew up — force-reset
    # restored idle anyway.
    assert loop.state is TurnState.IDLE
    resets = _events_of("fsm_force_reset")
    assert len(resets) == 1
    assert resets[0]["turn_id"] == say.turn_ids[0]
    assert resets[0]["from_state"] == "bob_speaking"
    assert resets[0]["fsm_reset"] is True
    assert "ws closed during finalize" in resets[0]["error"]
    # The 0117 metrics entry was closed + emitted by the force-reset (no leak).
    metrics = [m for m in _events_of("turn_metrics") if m.get("turn_id") == say.turn_ids[0]]
    assert len(metrics) == 1
    assert turn_metrics.get_default_collector().finish_turn(say.turn_ids[0]) is None

    # STT was re-armed: the next utterance runs end-to-end on the same loop.
    await _drive_turn(loop)
    assert say.transcripts == ["bonjour", "bonjour"]
    assert loop.state is TurnState.IDLE
    await loop.stop()


async def test_finalize_partial_failure_still_emits_reset_and_closes_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalize dies AFTER the FSM edge (mid-emit): cleanup still completes.

    The ``tts_end`` transition already landed (FSM idle) when the latency emit
    raises. The force-reset has nothing to reset (``fsm_reset`` false) but the
    abnormal path is still observable and the metrics entry still closes.
    """

    say = _RecordingSayPath(produce_audio=True)
    loop = _loop_for(say)
    assert await loop.start() is True

    async def _boom(turn_id: str) -> None:
        raise RuntimeError("event bus down")

    monkeypatch.setattr(loop, "_emit_turn_latency", _boom)
    await _drive_turn(loop)

    assert loop.state is TurnState.IDLE
    resets = _events_of("fsm_force_reset")
    assert len(resets) == 1
    assert resets[0]["fsm_reset"] is False
    # Metrics closed by the force-reset even though finalize died before its
    # own emit (issue 0117 — no in-flight entry leaks).
    assert turn_metrics.get_default_collector().finish_turn(say.turn_ids[0]) is None

    # The armed window stays usable (STT re-armed by the force-reset).
    await _drive_turn(loop)
    assert say.transcripts == ["bonjour", "bonjour"]
    assert loop.state is TurnState.IDLE
    await loop.stop()
