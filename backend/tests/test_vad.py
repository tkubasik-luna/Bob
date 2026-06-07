"""Unit tests for the energy VAD (PRD 0016 / issue 0100).

Threshold behaviour (loud frame → speech_start; sustained quiet → pause),
the fast-attack / release-hysteresis edges, the no-false-pause-inside-utterance
guard, and the RMS helper + ms→frames conversion.
"""

from __future__ import annotations

import struct

from bob.vad import EnergyVad, VadEvent, rms_normalised


def _frame(amplitude: int, *, samples: int = 480) -> bytes:
    """An s16le PCM payload (NO tag) of constant amplitude — RMS == |amp|/32768."""

    return struct.pack(f"<{samples}h", *([amplitude] * samples))


_LOUD = _frame(8000)  # rms ~0.24, well above default 0.02
_QUIET = _frame(0)  # silence


# --- rms_normalised ----------------------------------------------------------


def test_rms_silence_is_zero() -> None:
    assert rms_normalised(_QUIET) == 0.0


def test_rms_empty_is_zero() -> None:
    assert rms_normalised(b"") == 0.0


def test_rms_constant_amplitude() -> None:
    # Constant amplitude → RMS == amplitude, normalised by 32768.
    assert abs(rms_normalised(_frame(16384)) - (16384 / 32768.0)) < 1e-6


# --- speech onset (fast attack) ----------------------------------------------


def test_loud_frame_emits_speech_start_once() -> None:
    vad = EnergyVad(speech_rms=0.02, pause_frames=3)
    assert vad.feed_frame(_LOUD) is VadEvent.SPEECH_START
    # Subsequent loud frames do not re-emit (already speaking).
    assert vad.feed_frame(_LOUD) is None
    assert vad.speaking is True


def test_quiet_before_any_speech_is_silent() -> None:
    vad = EnergyVad(speech_rms=0.02, pause_frames=3)
    for _ in range(10):
        assert vad.feed_frame(_QUIET) is None
    assert vad.speaking is False


# --- pause (release hysteresis) ----------------------------------------------


def test_pause_after_pause_frames_quiet() -> None:
    vad = EnergyVad(speech_rms=0.02, pause_frames=3)
    assert vad.feed_frame(_LOUD) is VadEvent.SPEECH_START
    # Two quiet frames: not yet a pause (hysteresis = 3).
    assert vad.feed_frame(_QUIET) is None
    assert vad.feed_frame(_QUIET) is None
    # Third quiet frame crosses the floor → pause.
    assert vad.feed_frame(_QUIET) is VadEvent.PAUSE
    assert vad.speaking is False


def test_short_quiet_gap_does_not_emit_pause() -> None:
    """A single quiet frame inside an utterance must not spuriously pause."""

    vad = EnergyVad(speech_rms=0.02, pause_frames=3)
    vad.feed_frame(_LOUD)
    assert vad.feed_frame(_QUIET) is None  # 1 quiet
    assert vad.feed_frame(_QUIET) is None  # 2 quiet
    assert vad.feed_frame(_LOUD) is None  # speech resumes → run reset, no pause
    assert vad.speaking is True
    # The quiet run was reset; it takes another full 3 to pause.
    assert vad.feed_frame(_QUIET) is None
    assert vad.feed_frame(_QUIET) is None
    assert vad.feed_frame(_QUIET) is VadEvent.PAUSE


def test_speech_resumes_after_pause() -> None:
    vad = EnergyVad(speech_rms=0.02, pause_frames=2)
    vad.feed_frame(_LOUD)
    vad.feed_frame(_QUIET)
    assert vad.feed_frame(_QUIET) is VadEvent.PAUSE
    # New speech after a pause emits a fresh speech_start.
    assert vad.feed_frame(_LOUD) is VadEvent.SPEECH_START


# --- threshold sensitivity ---------------------------------------------------


def test_threshold_gate() -> None:
    # A frame just under threshold reads as silence; just over reads as speech.
    vad = EnergyVad(speech_rms=0.20, pause_frames=2)
    assert vad.feed_frame(_frame(6000)) is None  # ~0.18 < 0.20
    vad2 = EnergyVad(speech_rms=0.20, pause_frames=2)
    assert vad2.feed_frame(_frame(8000)) is VadEvent.SPEECH_START  # ~0.24 > 0.20


# --- observe() parity + helpers ----------------------------------------------


def test_observe_matches_feed_frame() -> None:
    vad = EnergyVad(speech_rms=0.02, pause_frames=2)
    assert vad.observe(is_speech=True) is VadEvent.SPEECH_START
    assert vad.observe(is_speech=False) is None
    assert vad.observe(is_speech=False) is VadEvent.PAUSE


def test_frames_for_ms() -> None:
    vad = EnergyVad(frame_ms=30)
    assert vad.frames_for_ms(600) == 20
    assert vad.frames_for_ms(0) == 1  # floored at 1
    assert vad.frames_for_ms(45) == 2  # rounds (1.5 → 2)


def test_pause_frames_floored_at_one() -> None:
    vad = EnergyVad(speech_rms=0.02, pause_frames=0)
    assert vad.pause_frames == 1
    vad.feed_frame(_LOUD)
    assert vad.feed_frame(_QUIET) is VadEvent.PAUSE  # one quiet frame suffices


def test_reset_clears_state() -> None:
    vad = EnergyVad(speech_rms=0.02, pause_frames=2)
    vad.feed_frame(_LOUD)
    assert vad.speaking is True
    vad.reset()
    assert vad.speaking is False
    # After reset a loud frame is a fresh onset.
    assert vad.feed_frame(_LOUD) is VadEvent.SPEECH_START
