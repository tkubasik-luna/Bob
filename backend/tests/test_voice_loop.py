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
from bob.speculative_draft import SpeculativeDraft
from bob.stt_engine import MIC_FRAME_TAG, FakeSttEngine
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
