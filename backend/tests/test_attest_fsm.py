"""Tests for the full-duplex attest extension (PRD 0016 / issue 0100).

Additive on the 0098/0099 harness:

1. the ``turn_state`` / ``audio_chunk`` logical matchers are registered;
2. the ``fsm_reached`` + ``audio_chunks_gte`` assertions (synthetic events);
3. the ``wait_state`` timeline op (against a stub capture — no real backend).
"""

from __future__ import annotations

from typing import Any

import pytest

from bob.attest.assertions import (
    LOGICAL_EVENT_MATCHERS,
    AssertionContext,
    known_kinds,
    run_assertion,
)
from bob.attest.runner import Scenario, ScenarioRunner


def _turn_state(to: str, *, frm: str = "idle", turn_id: str = "t1") -> dict[str, Any]:
    """A captured ``turn_state`` voice frame (emit_event nests under ws_event)."""

    return {
        "category": "voice",
        "payload": {
            "ws_event": {
                "type": "turn_state",
                "turn_id": turn_id,
                "from": frm,
                "to": to,
                "reason": "x",
            }
        },
    }


def _audio_chunk(index: int = 0) -> dict[str, Any]:
    return {
        "category": "voice",
        "payload": {"ws_event": {"type": "audio_chunk", "chunk_index": index}},
    }


def _ctx(*events: dict[str, Any]) -> AssertionContext:
    return AssertionContext(events=list(events), deliverable="")


# --- registration ------------------------------------------------------------


def test_kinds_and_matchers_registered() -> None:
    assert "fsm_reached" in known_kinds()
    assert "fsm_not_reached" in known_kinds()  # issue 0103
    assert "audio_chunks_gte" in known_kinds()
    assert "turn_state" in LOGICAL_EVENT_MATCHERS
    assert "audio_chunk" in LOGICAL_EVENT_MATCHERS


# --- fsm_reached -------------------------------------------------------------


def test_fsm_reached_pass() -> None:
    ctx = _ctx(
        _turn_state("user_speaking"),
        _turn_state("thinking", frm="user_speaking"),
        _turn_state("bob_speaking", frm="thinking"),
    )
    assert run_assertion({"kind": "fsm_reached", "state": "bob_speaking"}, ctx).ok is True


def test_fsm_reached_fail_when_state_not_visited() -> None:
    ctx = _ctx(_turn_state("user_speaking"))
    result = run_assertion({"kind": "fsm_reached", "state": "bob_speaking"}, ctx)
    assert result.ok is False
    assert result.detail["states_reached"] == ["user_speaking"]


def test_fsm_reached_requires_state() -> None:
    result = run_assertion({"kind": "fsm_reached"}, _ctx(_turn_state("idle")))
    assert result.ok is False
    assert "error" in result.detail


def test_fsm_reached_narrows_by_turn_id() -> None:
    ctx = _ctx(
        _turn_state("bob_speaking", turn_id="A"),
        _turn_state("user_speaking", turn_id="B"),
    )
    # bob_speaking only happened on turn A.
    assert (
        run_assertion({"kind": "fsm_reached", "state": "bob_speaking", "turn_id": "A"}, ctx).ok
        is True
    )
    assert (
        run_assertion({"kind": "fsm_reached", "state": "bob_speaking", "turn_id": "B"}, ctx).ok
        is False
    )


# --- fsm_not_reached (issue 0103) --------------------------------------------


def test_fsm_not_reached_pass_when_state_absent() -> None:
    # The hesitation case: the turn opened but never reached bob_speaking.
    ctx = _ctx(_turn_state("user_speaking"))
    assert run_assertion({"kind": "fsm_not_reached", "state": "bob_speaking"}, ctx).ok is True


def test_fsm_not_reached_fail_when_state_present() -> None:
    ctx = _ctx(
        _turn_state("user_speaking"),
        _turn_state("bob_speaking", frm="thinking"),
    )
    result = run_assertion({"kind": "fsm_not_reached", "state": "bob_speaking"}, ctx)
    assert result.ok is False
    assert "bob_speaking" in result.detail["states_reached"]


def test_fsm_not_reached_requires_state() -> None:
    result = run_assertion({"kind": "fsm_not_reached"}, _ctx(_turn_state("idle")))
    assert result.ok is False
    assert "error" in result.detail


def test_fsm_not_reached_narrows_by_turn_id() -> None:
    ctx = _ctx(
        _turn_state("bob_speaking", turn_id="A"),
        _turn_state("user_speaking", turn_id="B"),
    )
    # Turn B never reached bob_speaking → not_reached holds for B, fails for A.
    assert (
        run_assertion({"kind": "fsm_not_reached", "state": "bob_speaking", "turn_id": "B"}, ctx).ok
        is True
    )
    assert (
        run_assertion({"kind": "fsm_not_reached", "state": "bob_speaking", "turn_id": "A"}, ctx).ok
        is False
    )


# --- audio_chunks_gte --------------------------------------------------------


def test_audio_chunks_gte_pass() -> None:
    ctx = _ctx(_audio_chunk(0), _audio_chunk(1))
    assert run_assertion({"kind": "audio_chunks_gte", "min": 1}, ctx).ok is True
    assert run_assertion({"kind": "audio_chunks_gte", "min": 2}, ctx).ok is True


def test_audio_chunks_gte_fail() -> None:
    ctx = _ctx(_audio_chunk(0))
    result = run_assertion({"kind": "audio_chunks_gte", "min": 2}, ctx)
    assert result.ok is False
    assert result.detail == {"min": 2, "count": 1}
    assert result.to_dict() == {"kind": "audio_chunks_gte", "ok": False, "min": 2, "count": 1}


def test_audio_chunks_gte_default_min_is_one() -> None:
    assert run_assertion({"kind": "audio_chunks_gte"}, _ctx(_audio_chunk(0))).ok is True
    assert run_assertion({"kind": "audio_chunks_gte"}, _ctx()).ok is False


def test_audio_chunks_gte_bad_min() -> None:
    result = run_assertion({"kind": "audio_chunks_gte", "min": "lots"}, _ctx(_audio_chunk(0)))
    assert result.ok is False
    assert "error" in result.detail


# --- wait_state timeline op (stub capture) -----------------------------------


class _StubCapture:
    """Answers ``wait_for`` by evaluating the predicate against preset events."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.wait_calls: list[int] = []

    async def wait_for(self, predicate: Any, *, timeout_ms: int) -> bool:
        self.wait_calls.append(timeout_ms)
        return any(predicate(e) for e in self.events)


async def test_wait_state_observed() -> None:
    scenario = Scenario.from_dict(
        {
            "name": "x",
            "timeline": [{"do": "wait_state", "state": "bob_speaking", "timeout_ms": 50}],
        }
    )
    runner = ScenarioRunner(scenario)
    capture = _StubCapture([_turn_state("bob_speaking", frm="thinking")])
    errors: list[str] = []
    await runner._execute_timeline("ws://h", capture, errors)  # type: ignore[arg-type]
    assert errors == []
    assert capture.wait_calls == [50]


async def test_wait_state_timeout_is_loud() -> None:
    scenario = Scenario.from_dict(
        {"name": "x", "timeline": [{"do": "wait_state", "state": "bob_speaking"}]}
    )
    runner = ScenarioRunner(scenario)
    capture = _StubCapture([_turn_state("user_speaking")])  # never reaches bob_speaking
    errors: list[str] = []
    await runner._execute_timeline("ws://h", capture, errors)  # type: ignore[arg-type]
    assert len(errors) == 1
    assert "not reached" in errors[0]


async def test_wait_state_requires_state() -> None:
    scenario = Scenario.from_dict({"name": "x", "timeline": [{"do": "wait_state"}]})
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline("ws://h", _StubCapture([]), errors)  # type: ignore[arg-type]
    assert any("requires a 'state'" in e for e in errors)


@pytest.mark.parametrize("state", ["user_speaking", "thinking", "bob_speaking", "idle"])
async def test_wait_state_matches_each_state(state: str) -> None:
    scenario = Scenario.from_dict({"name": "x", "timeline": [{"do": "wait_state", "state": state}]})
    runner = ScenarioRunner(scenario)
    capture = _StubCapture([_turn_state(state)])
    errors: list[str] = []
    await runner._execute_timeline("ws://h", capture, errors)  # type: ignore[arg-type]
    assert errors == []
