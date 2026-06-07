"""Unit tests for the silence-floor Endpointer (PRD 0016 / issue 0100).

Silence sequences → endpoint decisions: arms only after speech, fires once the
silence run reaches the floor, fires exactly once, resets the run on resumed
speech, and the raw-PCM ``feed_frame`` parity path.
"""

from __future__ import annotations

import struct

from bob.endpointer import Endpointer


def _frame(amplitude: int, *, samples: int = 480) -> bytes:
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


_LOUD = _frame(8000)
_QUIET = _frame(0)


# --- arming ------------------------------------------------------------------


def test_silence_before_speech_never_ends() -> None:
    ep = Endpointer(silence_floor_frames=3)
    for _ in range(100):
        assert ep.observe(is_speech=False) is False
    assert ep.armed is False


def test_arms_on_first_speech() -> None:
    ep = Endpointer(silence_floor_frames=3)
    assert ep.observe(is_speech=True) is False
    assert ep.armed is True


# --- floor -------------------------------------------------------------------


def test_endpoint_fires_at_floor() -> None:
    ep = Endpointer(silence_floor_frames=3)
    ep.observe(is_speech=True)
    assert ep.observe(is_speech=False) is False  # 1
    assert ep.observe(is_speech=False) is False  # 2
    assert ep.observe(is_speech=False) is True  # 3 → endpoint


def test_endpoint_fires_exactly_once() -> None:
    ep = Endpointer(silence_floor_frames=2)
    ep.observe(is_speech=True)
    ep.observe(is_speech=False)
    assert ep.observe(is_speech=False) is True
    # Latched — further silence does not re-fire.
    for _ in range(10):
        assert ep.observe(is_speech=False) is False


def test_resumed_speech_resets_silence_run() -> None:
    ep = Endpointer(silence_floor_frames=3)
    ep.observe(is_speech=True)
    ep.observe(is_speech=False)  # 1
    ep.observe(is_speech=False)  # 2
    ep.observe(is_speech=True)  # resume → run cleared
    # Needs a fresh full run of 3 to fire.
    assert ep.observe(is_speech=False) is False
    assert ep.observe(is_speech=False) is False
    assert ep.observe(is_speech=False) is True


def test_floor_floored_at_one() -> None:
    ep = Endpointer(silence_floor_frames=0)
    assert ep.silence_floor_frames == 1
    ep.observe(is_speech=True)
    assert ep.observe(is_speech=False) is True


# --- raw-PCM path ------------------------------------------------------------


def test_feed_frame_computes_is_speech() -> None:
    ep = Endpointer(silence_floor_frames=2, speech_rms=0.02)
    assert ep.feed_frame(_LOUD) is False  # arms
    assert ep.feed_frame(_QUIET) is False  # 1
    assert ep.feed_frame(_QUIET) is True  # 2 → endpoint


def test_reset_disarms() -> None:
    ep = Endpointer(silence_floor_frames=2)
    ep.observe(is_speech=True)
    ep.observe(is_speech=False)
    ep.observe(is_speech=False)  # fired
    ep.reset()
    assert ep.armed is False
    # After reset, pre-speech silence is inert again.
    assert ep.observe(is_speech=False) is False
