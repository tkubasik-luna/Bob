"""Tests for the backchannel attest extension (PRD 0016 / issue 0105).

Additive on the 0098/0100/0101/0102 harness:

1. the ``backchannel`` logical matcher is registered;
2. the ``backchannel_emitted`` assertion (min + optional max, over synthetic
   captured events — fast, no backend);
3. the ``pause_count`` knob produces a mid-utterance silence beat in the
   synthesised voiced frames (so a ``vad_pause`` fires mid-turn).

The end-to-end runs live in ``scenarios/backchannel*.attest.yaml`` (driven by
``uv run bob attest``), kept out of the unit suite for speed.
"""

from __future__ import annotations

from typing import Any

from bob.attest.assertions import (
    LOGICAL_EVENT_MATCHERS,
    AssertionContext,
    known_kinds,
    run_assertion,
)
from bob.attest.drive import synth_voiced_frames
from bob.stt_engine import MIC_FRAME_TAG
from bob.vad import rms_normalised


def _backchannel(*, token: str = "mm", turn_id: str = "t1") -> dict[str, Any]:
    """A captured ``backchannel`` voice frame (nested under ws_event)."""

    return {
        "category": "voice",
        "payload": {
            "ws_event": {"type": "backchannel", "turn_id": turn_id, "token": token, "ts": 1.0}
        },
    }


def _ctx(*events: dict[str, Any]) -> AssertionContext:
    return AssertionContext(events=list(events), deliverable="")


# --- registration ------------------------------------------------------------


def test_kind_and_matcher_registered() -> None:
    assert "backchannel_emitted" in known_kinds()
    assert "backchannel" in LOGICAL_EVENT_MATCHERS


def test_backchannel_matcher_recognises_event() -> None:
    matcher = LOGICAL_EVENT_MATCHERS["backchannel"]
    assert matcher(_backchannel()) is True
    assert matcher({"category": "voice", "payload": {"ws_event": {"type": "bargein"}}}) is False


# --- backchannel_emitted: positive (min) -------------------------------------


def test_backchannel_emitted_pass() -> None:
    result = run_assertion({"kind": "backchannel_emitted", "min": 1}, _ctx(_backchannel()))
    assert result.ok is True
    assert result.detail["count"] == 1
    assert result.detail["tokens"] == ["mm"]


def test_backchannel_emitted_fail_when_none() -> None:
    result = run_assertion({"kind": "backchannel_emitted", "min": 1}, _ctx())
    assert result.ok is False
    assert result.detail["count"] == 0


# --- backchannel_emitted: negative (max: 0) ----------------------------------


def test_backchannel_emitted_max_zero_pass_when_none() -> None:
    # The "no backchannel over continuous speech" assertion: max 0 passes on none.
    result = run_assertion({"kind": "backchannel_emitted", "min": 0, "max": 0}, _ctx())
    assert result.ok is True


def test_backchannel_emitted_max_zero_fail_when_one() -> None:
    result = run_assertion(
        {"kind": "backchannel_emitted", "min": 0, "max": 0}, _ctx(_backchannel())
    )
    assert result.ok is False
    assert result.detail["count"] == 1


def test_backchannel_emitted_bad_min() -> None:
    result = run_assertion({"kind": "backchannel_emitted", "min": "lots"}, _ctx(_backchannel()))
    assert result.ok is False
    assert "error" in result.detail


def test_backchannel_emitted_bad_max() -> None:
    result = run_assertion({"kind": "backchannel_emitted", "max": "none"}, _ctx(_backchannel()))
    assert result.ok is False
    assert "error" in result.detail


# --- pause_count knob (mid-utterance silence beat) ---------------------------


def _is_loud(frame: bytes) -> bool:
    # Strip the mic tag byte, measure RMS like the loop does.
    return rms_normalised(frame[1:]) >= 0.02


def test_pause_count_inserts_mid_utterance_silence() -> None:
    frames = synth_voiced_frames(voiced_count=10, silence_count=8, pause_count=4)
    # Every frame still carries the mic tag.
    assert all(f[0] == MIC_FRAME_TAG for f in frames)
    loud = [_is_loud(f) for f in frames]
    # The pattern is voiced / SILENCE beat / voiced / trailing silence — i.e. a
    # run of quiet frames appears BETWEEN two runs of loud frames (the mid beat),
    # which the single-burst layout (pause_count=0) never has.
    assert loud[0] is True  # opens loud
    # There is a True after the first False (speech resumes after the mid beat).
    first_false = loud.index(False)
    assert any(loud[first_false:]) is True


def test_pause_count_zero_is_single_burst() -> None:
    frames = synth_voiced_frames(voiced_count=6, silence_count=5, pause_count=0)
    loud = [_is_loud(f) for f in frames]
    # Single burst: all loud frames come first, then only silence — no resume.
    first_false = loud.index(False)
    assert not any(loud[first_false:])
