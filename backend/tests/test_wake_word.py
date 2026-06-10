"""Wake-word tests — « Yo Bob » standby → écoute (:mod:`bob.wake_word`).

Three layers:

- the pure matcher/stripper (fuzzy hits on the small model's mishearings,
  prefix-only stripping);
- the :class:`WakeWordDetector` gates (VAD speech requirement, cadence
  debounce, rolling window seed);
- the :class:`bob.voice_loop.FullDuplexLoop` integration: standby opens no
  turn, a wake hit opens one (turn_state → ``user_speaking`` — the orb's
  « écoute »), the wake phrase is stripped from the say-path transcript, a
  bare « Yo Bob » short-circuits to the acknowledgement, and the awake grace
  window lets a follow-up skip the phrase.
"""

from __future__ import annotations

import struct
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from bob import debug_log, turn_metrics
from bob.config import Settings
from bob.stt_engine import MIC_FRAME_TAG, FakeSttEngine
from bob.turn_fsm import TurnState
from bob.voice_loop import FullDuplexLoop
from bob.voice_turn import VoiceTurn
from bob.wake_word import (
    WakeWordDetector,
    match_wake_phrase,
    normalise_text,
    strip_wake_prefix,
)


def _frame(amplitude: int, *, samples: int = 480) -> bytes:
    return bytes([MIC_FRAME_TAG]) + struct.pack(f"<{samples}h", *([amplitude] * samples))


def _fsm_state(loop: FullDuplexLoop) -> TurnState:
    """Read the FSM state through a call boundary.

    An earlier ``assert loop.state is TurnState.USER_SPEAKING`` narrows the
    property to that literal for the rest of the test in mypy's eyes (awaited
    method calls do not invalidate member narrowing), so a later direct
    ``loop.state is TurnState.IDLE`` is flagged ``comparison-overlap``. The
    call boundary resets the inferred type to ``TurnState``.
    """

    return loop.state


_LOUD = _frame(8000)
_QUIET = _frame(0)


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    debug_log.clear()
    turn_metrics.set_default_collector(None)


# --- matcher -----------------------------------------------------------------


def test_normalise_folds_accents_case_punctuation() -> None:
    assert normalise_text("  Yo, Bob !  Quelle heure est-il ?") == "yo bob quelle heure est il"


def test_match_exact_phrase() -> None:
    match = match_wake_phrase("Yo Bob, quelle heure est-il ?", "yo bob")
    assert match is not None
    assert match.score == 1.0


def test_match_small_model_mishearing() -> None:
    # The real tiny-model output for a spoken « Yo Bob » (observed): fuzzy hit.
    assert match_wake_phrase("Yobab, qu'elle aura tille ?", "yo bob") is not None


def test_no_match_on_unrelated_speech() -> None:
    assert match_wake_phrase("bonjour le monde", "yo bob") is None
    assert match_wake_phrase("il fait beau aujourd'hui", "yo bob") is None


def test_no_match_on_empty() -> None:
    assert match_wake_phrase("", "yo bob") is None
    assert match_wake_phrase("yo bob", "") is None


# --- stripper ----------------------------------------------------------------


def test_strip_wake_prefix_with_command() -> None:
    assert strip_wake_prefix("Yo Bob, quelle heure est-il ?", "yo bob") == "quelle heure est-il ?"


def test_strip_wake_prefix_misheard() -> None:
    assert strip_wake_prefix("Yobab quelle heure", "yo bob") == "quelle heure"


def test_strip_bare_phrase_returns_empty() -> None:
    assert strip_wake_prefix("Yo Bob", "yo bob") == ""
    assert strip_wake_prefix("Yo Bob !", "yo bob") == ""


def test_strip_leaves_mid_sentence_phrase_alone() -> None:
    text = "quelle heure est-il yo bob"
    assert strip_wake_prefix(text, "yo bob") == text


def test_strip_leaves_unrelated_text_alone() -> None:
    assert strip_wake_prefix("bonjour le monde", "yo bob") == "bonjour le monde"


# --- detector ----------------------------------------------------------------


def _detector(
    transcriber: Callable[[bytes], str],
    *,
    interval_seconds: float = 0.06,
    min_speech_frames: int = 3,
) -> WakeWordDetector:
    return WakeWordDetector(
        transcriber=transcriber,
        phrase="yo bob",
        sample_rate=16_000,
        window_seconds=0.5,
        interval_seconds=interval_seconds,
        min_speech_frames=min_speech_frames,
    )


async def test_detector_never_transcribes_silence() -> None:
    calls: list[bytes] = []

    def transcriber(pcm: bytes) -> str:
        calls.append(pcm)
        return "yo bob"

    det = _detector(transcriber)
    for _ in range(20):
        assert await det.feed(b"\x00" * 960, is_speech=False) is None
    assert calls == []


async def test_detector_fires_on_speech_and_matches() -> None:
    def transcriber(_: bytes) -> str:
        return "Yobab !"

    det = _detector(transcriber)
    pcm = b"\x10" * 960  # content irrelevant — the transcriber is scripted
    match = None
    for _ in range(5):
        match = await det.feed(pcm, is_speech=True)
        if match is not None:
            break
    assert match is not None
    assert match.text == "Yobab !"


async def test_detector_ring_is_bounded() -> None:
    det = _detector(lambda _: "", min_speech_frames=10_000)  # never passes
    for _ in range(100):
        await det.feed(b"\x01" * 960, is_speech=True)
    # window_seconds=0.5 @16k = 8000 samples = 16000 bytes max (± one frame).
    assert len(det.recent_audio()) <= 16_000 + 960


async def test_detector_reset_clears_ring() -> None:
    det = _detector(lambda _: "")
    await det.feed(b"\x01" * 960, is_speech=True)
    det.reset()
    assert det.recent_audio() == b""


# --- loop integration ----------------------------------------------------------


class _RecordingSayPath:
    def __init__(self) -> None:
        self.transcripts: list[str] = []
        self.prepared_replies: list[str | None] = []

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
        self.prepared_replies.append(prepared_reply)
        if transcript.strip():
            await on_first_audio()


def _settings(**overrides: Any) -> Settings:
    return Settings.model_construct(
        STT_ENGINE="fake",
        STT_SAMPLE_RATE=16_000,
        VAD_SPEECH_RMS=0.02,
        VAD_PAUSE_MS=60,
        ENDPOINT_SILENCE_MS=120,
        STT_DEBUG_TEXT_MAX_CHARS=64,
        BACKCHANNEL_MIN_INTERVAL_MS=0,
        **overrides,
    )


def _wake_loop(
    say: _RecordingSayPath,
    *,
    transcript: str,
    wake_text: str = "yo bob",
    settings: Settings | None = None,
) -> tuple[FullDuplexLoop, list[bytes]]:
    """A loop with a scripted wake detector; returns (loop, transcriber calls)."""

    resolved = settings or _settings()
    calls: list[bytes] = []

    def transcriber(pcm: bytes) -> str:
        calls.append(pcm)
        return wake_text

    detector = WakeWordDetector(
        transcriber=transcriber,
        phrase=resolved.WAKE_WORD_PHRASE,
        sample_rate=16_000,
        window_seconds=0.5,
        interval_seconds=0.06,
        threshold=resolved.WAKE_WORD_MATCH_THRESHOLD,
    )
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript=transcript, samples_per_word=160),
            session_id="s1",
            settings=resolved,
        ),
        say_path=say,
        settings=resolved,
        session_id="s1",
        wake_detector=detector,
    )
    return loop, calls


def _wake_events() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "wake_word":
            out.append(ws_event)
    return out


async def test_standby_speech_opens_no_turn_without_wake() -> None:
    say = _RecordingSayPath()
    loop, _ = _wake_loop(say, transcript="bonjour", wake_text="rien à voir")
    assert await loop.start() is True

    for _ in range(10):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    assert loop.state is TurnState.IDLE
    assert say.transcripts == []
    assert _wake_events() == []
    await loop.stop()


async def test_wake_opens_turn_and_strips_phrase_from_say_path() -> None:
    say = _RecordingSayPath()
    loop, calls = _wake_loop(say, transcript="yo bob quelle heure est-il")
    assert await loop.start() is True

    # Standby: the first frames feed only the detector; the wake hit opens the
    # turn (idle → user_speaking — the orb's « écoute »).
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
        if loop.state is TurnState.USER_SPEAKING:
            break
    assert loop.state is TurnState.USER_SPEAKING
    assert calls, "the detector should have run at least one pass"
    assert len(_wake_events()) == 1

    # More speech then silence: endpoint → say-path on the STRIPPED command.
    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    assert say.transcripts == ["quelle heure est-il"]
    assert _fsm_state(loop) is TurnState.IDLE
    await loop.stop()


async def test_bare_wake_phrase_speaks_the_ack() -> None:
    say = _RecordingSayPath()
    loop, _ = _wake_loop(say, transcript="yo bob")
    assert await loop.start() is True

    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
        if loop.state is TurnState.USER_SPEAKING:
            break
    assert loop.state is TurnState.USER_SPEAKING
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    # The brain is skipped: the configured acknowledgement is the prepared reply.
    assert say.prepared_replies == ["Oui ?"]
    assert _fsm_state(loop) is TurnState.IDLE
    await loop.stop()


async def test_bare_wake_phrase_without_ack_ends_turn_silently() -> None:
    say = _RecordingSayPath()
    loop, _ = _wake_loop(say, transcript="yo bob", settings=_settings(WAKE_WORD_ACK_REPLY=""))
    assert await loop.start() is True

    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
        if loop.state is TurnState.USER_SPEAKING:
            break
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()

    assert say.transcripts == []
    assert loop.state is TurnState.IDLE
    await loop.stop()


async def test_awake_window_lets_follow_up_skip_the_phrase() -> None:
    say = _RecordingSayPath()
    # The detector only ever hears the wake on its first pass; the follow-up
    # turn must open WITHOUT it (the awake grace window).
    loop, calls = _wake_loop(say, transcript="yo bob quelle heure est-il")
    assert await loop.start() is True

    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
        if loop.state is TurnState.USER_SPEAKING:
            break
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert say.transcripts == ["quelle heure est-il"]

    passes_after_first_turn = len(calls)
    # Follow-up burst inside the awake window: a turn opens straight away —
    # no standby, no detector pass.
    for _ in range(4):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.USER_SPEAKING
    assert len(calls) == passes_after_first_turn
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert len(say.transcripts) == 2
    await loop.stop()


async def test_lapsed_awake_window_returns_to_standby() -> None:
    say = _RecordingSayPath()
    # Zero grace: the loop is back in standby the instant a turn ends. The
    # detector never matches again (wake_text flips after the first pass).
    state = {"first": True}

    def transcriber(_: bytes) -> str:
        if state["first"]:
            state["first"] = False
            return "yo bob"
        return "rien"

    settings = _settings(WAKE_WORD_AWAKE_WINDOW_SECONDS=0.0)
    detector = WakeWordDetector(
        transcriber=transcriber,
        phrase="yo bob",
        sample_rate=16_000,
        window_seconds=0.5,
        interval_seconds=0.06,
    )
    loop = FullDuplexLoop(
        voice_turn_factory=lambda: VoiceTurn(
            engine=FakeSttEngine(transcript="yo bob bonjour", samples_per_word=160),
            session_id="s1",
            settings=settings,
        ),
        say_path=say,
        settings=settings,
        session_id="s1",
        wake_detector=detector,
    )
    assert await loop.start() is True

    for _ in range(6):
        await loop.feed_raw_frame(_LOUD)
        if loop.state is TurnState.USER_SPEAKING:
            break
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert say.transcripts == ["bonjour"]

    # Grace lapsed (0 s): a new burst stays in standby — no turn, no say-path.
    for _ in range(10):
        await loop.feed_raw_frame(_LOUD)
    assert loop.state is TurnState.IDLE
    for _ in range(10):
        await loop.feed_raw_frame(_QUIET)
    await loop.join()
    assert len(say.transcripts) == 1
    await loop.stop()
