"""Tests for the Listen / STT attest extension (PRD 0016 / issue 0099).

Three layers:

1. ``synth_mic_frames`` produces correctly-tagged, decodable mic frames.
2. The ``stt_final_matches`` assertion (synthetic captured events — fast).
3. An end-to-end ``bob attest`` run of the ``--audio`` path against an ephemeral
   backend whose STT engine is the deterministic fake (no native whisper).
"""

from __future__ import annotations

from bob.attest.assertions import (
    LOGICAL_EVENT_MATCHERS,
    AssertionContext,
    known_kinds,
    run_assertion,
)
from bob.attest.drive import synth_mic_frames
from bob.attest.runner import Scenario, ScenarioRunner
from bob.stt_engine import MIC_FRAME_TAG, decode_pcm_frame

# --- synthetic mic frames ----------------------------------------------------


def test_synth_mic_frames_are_tagged_and_decodable() -> None:
    frames = synth_mic_frames(frame_count=3, samples_per_frame=480)
    assert len(frames) == 3
    for frame in frames:
        assert frame[0] == MIC_FRAME_TAG
        pcm = decode_pcm_frame(frame)
        assert len(pcm) == 480 * 2  # 480 s16le samples


def test_synth_mic_frames_floor_of_one() -> None:
    assert len(synth_mic_frames(frame_count=0)) == 1


# --- stt_final_matches assertion (synthetic events) --------------------------


def _voice_final(text: str) -> dict[str, object]:
    # emit_event nests the wire payload under payload.ws_event (see event_bus_v2).
    return {"category": "voice", "payload": {"ws_event": {"type": "stt_final", "text": text}}}


def _ctx(*events: dict[str, object]) -> AssertionContext:
    return AssertionContext(events=list(events), deliverable="")


def test_assertion_and_matchers_registered() -> None:
    assert "stt_final_matches" in known_kinds()
    assert "stt_final" in LOGICAL_EVENT_MATCHERS
    assert "stt_partial" in LOGICAL_EVENT_MATCHERS


def test_stt_final_matches_contains_pass() -> None:
    result = run_assertion(
        {"kind": "stt_final_matches", "contains": "bonjour"}, _ctx(_voice_final("bonjour paris"))
    )
    assert result.ok is True


def test_stt_final_matches_contains_fail() -> None:
    result = run_assertion(
        {"kind": "stt_final_matches", "contains": "lyon"}, _ctx(_voice_final("bonjour paris"))
    )
    assert result.ok is False


def test_stt_final_matches_regex() -> None:
    result = run_assertion(
        {"kind": "stt_final_matches", "regex": r"bon\w+"}, _ctx(_voice_final("bonjour"))
    )
    assert result.ok is True


def test_stt_final_matches_no_final_event() -> None:
    result = run_assertion(
        {"kind": "stt_final_matches", "contains": "x"},
        _ctx({"category": "output", "payload": {"speech": "salut"}}),
    )
    assert result.ok is False
    assert "no stt_final" in result.detail["error"]


def test_stt_final_matches_requires_criterion() -> None:
    result = run_assertion({"kind": "stt_final_matches"}, _ctx(_voice_final("bonjour")))
    assert result.ok is False
    assert "error" in result.detail


# --- end-to-end over an ephemeral backend (subprocess, fake STT) -------------


def test_audio_scenario_end_to_end() -> None:
    """A full ``inject_audio`` → ``stt_final`` run over the real binary WS.

    Boots an isolated backend with the fake STT engine scripted to
    ``bonjour paris`` (no native model), streams synthetic mic frames, and
    asserts the captured ``stt_final`` contains the expected word.
    """

    scenario = Scenario.from_dict(
        {
            "name": "listen-stt-final-test",
            "backend": "ephemeral",
            "llm": "fake",
            "timeline": [
                {"do": "inject_audio", "transcript": "bonjour paris"},
                {"do": "wait_event", "type": "stt_final", "timeout_ms": 8000},
            ],
            "assertions": [
                {"kind": "stt_final_matches", "contains": "bonjour"},
                {"kind": "no_error_events"},
            ],
        }
    )
    verdict = ScenarioRunner(scenario).run()
    assert verdict["ok"] is True, verdict
    assert verdict["events_captured"] > 0
