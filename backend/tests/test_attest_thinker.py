"""Tests for the Thinker attest extension (PRD 0016 / issue 0102).

Additive on the 0098/0100/0101 harness:

1. the ``thinker_snapshot`` / ``thinker_consult`` logical matchers are registered;
2. the ``thinker_snapshot_emitted`` + ``speaker_consulted_thinker`` assertions
   (synthetic captured events — fast, no backend);
3. the ``thinker: true`` flag derives ``THINKER_DEBOUNCE_MS`` in the harness env.

The end-to-end run lives in ``scenarios/thinker.attest.yaml`` (driven by
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
from bob.attest.runner import Scenario, ScenarioRunner


def _snapshot(*, seq: int = 1, turn_id: str = "t1") -> dict[str, Any]:
    """A captured ``thinker_snapshot`` voice frame (nested under ws_event)."""

    return {
        "category": "voice",
        "payload": {
            "ws_event": {
                "type": "thinker_snapshot",
                "turn_id": turn_id,
                "seq": seq,
                "corrected_text": "quel temps",
                "user_turn_complete": False,
            }
        },
    }


def _consult(*, seq: int = 1, turn_id: str = "t1") -> dict[str, Any]:
    return {
        "category": "voice",
        "payload": {"ws_event": {"type": "thinker_consult", "turn_id": turn_id, "seq": seq}},
    }


def _ctx(*events: dict[str, Any]) -> AssertionContext:
    return AssertionContext(events=list(events), deliverable="")


# --- registration ------------------------------------------------------------


def test_kinds_and_matchers_registered() -> None:
    assert "thinker_snapshot_emitted" in known_kinds()
    assert "speaker_consulted_thinker" in known_kinds()
    assert "thinker_snapshot" in LOGICAL_EVENT_MATCHERS
    assert "thinker_consult" in LOGICAL_EVENT_MATCHERS


def test_thinker_snapshot_matcher_recognises_event() -> None:
    matcher = LOGICAL_EVENT_MATCHERS["thinker_snapshot"]
    assert matcher(_snapshot()) is True
    assert matcher({"category": "voice", "payload": {"ws_event": {"type": "stt_final"}}}) is False


# --- thinker_snapshot_emitted ------------------------------------------------


def test_thinker_snapshot_emitted_pass() -> None:
    ctx = _ctx(_snapshot(seq=1), _snapshot(seq=2))
    result = run_assertion({"kind": "thinker_snapshot_emitted"}, ctx)
    assert result.ok is True
    assert result.detail["count"] == 2


def test_thinker_snapshot_emitted_fail_when_none() -> None:
    result = run_assertion({"kind": "thinker_snapshot_emitted"}, _ctx())
    assert result.ok is False
    assert result.detail["count"] == 0


def test_thinker_snapshot_emitted_min() -> None:
    ctx = _ctx(_snapshot(seq=1))
    assert run_assertion({"kind": "thinker_snapshot_emitted", "min": 2}, ctx).ok is False
    assert run_assertion({"kind": "thinker_snapshot_emitted", "min": 1}, ctx).ok is True


def test_thinker_snapshot_emitted_bad_min() -> None:
    result = run_assertion({"kind": "thinker_snapshot_emitted", "min": "lots"}, _ctx(_snapshot()))
    assert result.ok is False
    assert "error" in result.detail


# --- speaker_consulted_thinker -----------------------------------------------


def test_speaker_consulted_thinker_pass() -> None:
    ctx = _ctx(_snapshot(seq=2), _consult(seq=2))
    result = run_assertion({"kind": "speaker_consulted_thinker"}, ctx)
    assert result.ok is True
    assert result.detail["consults"] == 1
    assert result.detail["seqs"] == [2]


def test_speaker_consulted_thinker_fail_when_no_marker() -> None:
    # A snapshot fired but the Speaker never folded it into the prompt.
    result = run_assertion({"kind": "speaker_consulted_thinker"}, _ctx(_snapshot()))
    assert result.ok is False
    assert result.detail["consults"] == 0


# --- thinker: true env derivation --------------------------------------------


def test_thinker_flag_pins_debounce_env() -> None:
    scenario = Scenario.from_dict(
        {
            "name": "x",
            "timeline": [
                {"do": "inject_audio", "transcript": "quel temps", "voiced": True, "thinker": True}
            ],
        }
    )
    env = ScenarioRunner(scenario)._extra_env()
    assert env["THINKER_DEBOUNCE_MS"] == "0"


def test_no_thinker_env_without_flag() -> None:
    scenario = Scenario.from_dict(
        {"name": "x", "timeline": [{"do": "inject_audio", "transcript": "hi", "voiced": True}]}
    )
    assert "THINKER_DEBOUNCE_MS" not in ScenarioRunner(scenario)._extra_env()


def test_bargein_env_still_derived_alongside_thinker() -> None:
    """The 0101 barge-in env keys still derive (no regression from the 0102 merge)."""

    scenario = Scenario.from_dict(
        {
            "name": "x",
            "timeline": [{"do": "inject_bargein", "transcript": "quel temps", "confirm_ms": 150}],
        }
    )
    env = ScenarioRunner(scenario)._extra_env()
    assert env["BARGEIN_CONFIRM_MS"] == "150"
