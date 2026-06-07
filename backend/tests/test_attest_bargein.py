"""Tests for the barge-in attest extension (PRD 0016 / issue 0101).

Additive on the 0098/0100 harness:

1. the ``bargein`` logical matcher is registered;
2. the ``bargein_within_ms`` + ``committed_equals_spoken`` assertions
   (synthetic captured events — fast, no backend);
3. the ``inject_bargein`` timeline op parses + derives the harness env.

The end-to-end run lives in the ``scenarios/bargein.attest.yaml`` scenario
(driven by ``uv run bob attest``), kept out of the unit suite for speed.
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


def _bargein(
    *,
    detected_ts: float,
    cut_ts: float,
    committed: str = "Bonjour",
    turn_id: str = "t1",
) -> dict[str, Any]:
    """A captured ``bargein`` voice frame (emit_event nests under ws_event)."""

    return {
        "category": "voice",
        "payload": {
            "ws_event": {
                "type": "bargein",
                "turn_id": turn_id,
                "detected_ts": detected_ts,
                "cut_ts": cut_ts,
                "committed_spoken_text": committed,
            }
        },
    }


def _ctx(*events: dict[str, Any], deliverable: str = "") -> AssertionContext:
    return AssertionContext(events=list(events), deliverable=deliverable)


# --- registration ------------------------------------------------------------


def test_kinds_and_matcher_registered() -> None:
    assert "bargein_within_ms" in known_kinds()
    assert "committed_equals_spoken" in known_kinds()
    assert "bargein" in LOGICAL_EVENT_MATCHERS


def test_bargein_matcher_recognises_event() -> None:
    matcher = LOGICAL_EVENT_MATCHERS["bargein"]
    assert matcher(_bargein(detected_ts=0.0, cut_ts=0.2)) is True
    assert matcher({"category": "voice", "payload": {"ws_event": {"type": "turn_state"}}}) is False


# --- bargein_within_ms -------------------------------------------------------


def test_bargein_within_ms_pass() -> None:
    ctx = _ctx(_bargein(detected_ts=1.000, cut_ts=1.201))  # 201 ms
    result = run_assertion({"kind": "bargein_within_ms", "max": 300}, ctx)
    assert result.ok is True
    assert result.detail["actual"] == 201.0


def test_bargein_within_ms_fail_when_too_slow() -> None:
    ctx = _ctx(_bargein(detected_ts=1.0, cut_ts=1.412))  # 412 ms
    result = run_assertion({"kind": "bargein_within_ms", "max": 300}, ctx)
    assert result.ok is False
    assert result.detail == {"expected_max": 300.0, "actual": 412.0}


def test_bargein_within_ms_fail_when_no_event() -> None:
    result = run_assertion({"kind": "bargein_within_ms", "max": 300}, _ctx())
    assert result.ok is False
    assert "no bargein event" in result.detail["error"]


def test_bargein_within_ms_picks_fastest_cut() -> None:
    ctx = _ctx(
        _bargein(detected_ts=0.0, cut_ts=0.5, turn_id="a"),  # 500 ms
        _bargein(detected_ts=1.0, cut_ts=1.21, turn_id="b"),  # 210 ms
    )
    result = run_assertion({"kind": "bargein_within_ms", "max": 300}, ctx)
    assert result.ok is True
    assert result.detail["actual"] == 210.0


def test_bargein_within_ms_bad_max() -> None:
    ctx = _ctx(_bargein(detected_ts=0.0, cut_ts=0.2))
    result = run_assertion({"kind": "bargein_within_ms", "max": "soon"}, ctx)
    assert result.ok is False
    assert "error" in result.detail


# --- committed_equals_spoken -------------------------------------------------


def test_committed_equals_spoken_prefix_pass() -> None:
    ctx = _ctx(
        _bargein(detected_ts=0.0, cut_ts=0.2, committed="Bonjour"),
        deliverable="Bonjour, il fait beau aujourd'hui.",
    )
    assert run_assertion({"kind": "committed_equals_spoken"}, ctx).ok is True


def test_committed_equals_spoken_full_equality_pass() -> None:
    ctx = _ctx(
        _bargein(detected_ts=0.0, cut_ts=0.2, committed="Bonjour le monde"),
        deliverable="Bonjour le monde",
    )
    assert run_assertion({"kind": "committed_equals_spoken"}, ctx).ok is True


def test_committed_equals_spoken_handles_scrub_elision() -> None:
    # The /ws/debug copy elides long text ("<window>… [+N chars]"); the verbatim
    # leading window must still match the deliverable prefix.
    ctx = _ctx(
        _bargein(detected_ts=0.0, cut_ts=0.2, committed="Bonjour, je suis… [+12 chars]"),
        deliverable="Bonjour, je suis Bob ton assistant.",
    )
    assert run_assertion({"kind": "committed_equals_spoken"}, ctx).ok is True


def test_committed_equals_spoken_fail_when_not_a_prefix() -> None:
    ctx = _ctx(
        _bargein(detected_ts=0.0, cut_ts=0.2, committed="Au revoir"),
        deliverable="Bonjour le monde",
    )
    assert run_assertion({"kind": "committed_equals_spoken"}, ctx).ok is False


def test_committed_equals_spoken_fail_when_committed_empty() -> None:
    ctx = _ctx(
        _bargein(detected_ts=0.0, cut_ts=0.2, committed=""),
        deliverable="Bonjour",
    )
    result = run_assertion({"kind": "committed_equals_spoken"}, ctx)
    assert result.ok is False
    assert "empty" in result.detail["error"]


def test_committed_equals_spoken_fail_when_no_bargein() -> None:
    result = run_assertion({"kind": "committed_equals_spoken"}, _ctx(deliverable="Bonjour"))
    assert result.ok is False
    assert "no bargein event" in result.detail["error"]


# --- inject_bargein timeline op + extra_env ----------------------------------


def test_inject_bargein_extra_env_derived() -> None:
    scenario = Scenario.from_dict(
        {
            "name": "x",
            "timeline": [
                {
                    "do": "inject_bargein",
                    "transcript": "quel temps",
                    "confirm_ms": 150,
                    "tts_chunk_ms": 50,
                    "tts_chunks": 5,
                }
            ],
        }
    )
    env = ScenarioRunner(scenario)._extra_env()
    assert env["BARGEIN_CONFIRM_MS"] == "150"
    assert env["BOB_FAKE_TTS_CHUNK_MS"] == "50"
    assert env["BOB_FAKE_TTS_CHUNKS"] == "5"


def test_inject_bargein_requires_transcript() -> None:
    scenario = Scenario.from_dict({"name": "x", "timeline": [{"do": "inject_bargein"}]})
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    import asyncio

    asyncio.run(runner._execute_timeline("ws://h", _StubCapture(), errors))  # type: ignore[arg-type]
    assert any("transcript" in e for e in errors)


def test_no_extra_env_without_bargein_step() -> None:
    scenario = Scenario.from_dict(
        {"name": "x", "timeline": [{"do": "inject_audio", "transcript": "hi"}]}
    )
    assert ScenarioRunner(scenario)._extra_env() == {}


class _StubCapture:
    """Minimal capture stub (inject_bargein never reaches wait_for here)."""

    async def wait_for(self, predicate: Any, *, timeout_ms: int) -> bool:  # pragma: no cover
        return False
