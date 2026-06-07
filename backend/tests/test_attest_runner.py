"""Unit tests for the attestation ScenarioRunner (issue 0098).

Covers YAML/dict parsing (valid + malformed), the unsupported-feature guard,
the Annexe C verdict assembly, and timeline-op dispatch (``inject_text`` /
``wait_event`` / ``wait_ms`` + loud handling of unimplemented ops) — the latter
exercised against a stub capture so no real backend boots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bob.attest.runner import (
    Scenario,
    ScenarioError,
    ScenarioRunner,
    build_verdict,
)

# --- Scenario.from_dict / from_yaml_file -------------------------------------


def test_from_dict_parses_full_scenario() -> None:
    scenario = Scenario.from_dict(
        {
            "name": "demo",
            "description": "d",
            "backend": "ephemeral",
            "llm": "fake",
            "fake_llm": [{"role": "jarvis", "reply": "hi"}],
            "timeline": [{"do": "inject_text", "text": "hello"}],
            "assertions": [{"kind": "event_emitted", "type": "say"}],
        }
    )
    assert scenario.name == "demo"
    assert scenario.fake_llm == [{"role": "jarvis", "reply": "hi"}]
    assert scenario.timeline[0]["do"] == "inject_text"
    assert scenario.assertions[0]["kind"] == "event_emitted"


def test_from_dict_defaults_backend_and_llm() -> None:
    scenario = Scenario.from_dict({"name": "x", "timeline": []})
    assert scenario.backend == "ephemeral"
    assert scenario.llm == "fake"
    assert scenario.fake_llm == []
    assert scenario.assertions == []


def test_from_dict_requires_name() -> None:
    with pytest.raises(ScenarioError, match="non-empty 'name'"):
        Scenario.from_dict({"timeline": []})


def test_from_dict_rejects_non_mapping() -> None:
    with pytest.raises(ScenarioError, match="mapping at the top level"):
        Scenario.from_dict([1, 2, 3])


def test_from_dict_rejects_non_list_timeline() -> None:
    with pytest.raises(ScenarioError, match="'timeline' must be a list"):
        Scenario.from_dict({"name": "x", "timeline": "nope"})


def test_from_dict_filters_non_dict_timeline_steps() -> None:
    scenario = Scenario.from_dict(
        {"name": "x", "timeline": [{"do": "wait_ms", "ms": 1}, "junk", 5]}
    )
    assert scenario.timeline == [{"do": "wait_ms", "ms": 1}]


def test_from_yaml_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "name: yaml-demo\n"
        "timeline:\n"
        "  - do: inject_text\n"
        "    text: bonjour\n"
        "assertions:\n"
        "  - kind: deliverable_nonempty\n",
        encoding="utf-8",
    )
    scenario = Scenario.from_yaml_file(path)
    assert scenario.name == "yaml-demo"
    assert scenario.timeline[0] == {"do": "inject_text", "text": "bonjour"}


def test_from_yaml_file_invalid_yaml_raises_scenario_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("name: x\n  : : : broken", encoding="utf-8")
    with pytest.raises(ScenarioError, match="invalid YAML"):
        Scenario.from_yaml_file(path)


# --- unsupported-feature guard -----------------------------------------------


def test_runner_rejects_external_backend() -> None:
    scenario = Scenario.from_dict({"name": "x", "backend": "external", "timeline": []})
    with pytest.raises(ScenarioError, match="backend 'external' not supported"):
        ScenarioRunner(scenario)


def test_runner_rejects_real_llm() -> None:
    scenario = Scenario.from_dict({"name": "x", "llm": "real", "timeline": []})
    with pytest.raises(ScenarioError, match="llm 'real' not supported"):
        ScenarioRunner(scenario)


# --- verdict assembly --------------------------------------------------------


def test_build_verdict_matches_annexe_c_shape() -> None:
    scenario = Scenario.from_dict({"name": "bargein-demo", "llm": "fake", "timeline": []})
    verdict = build_verdict(
        scenario,
        ok=False,
        duration_ms=1840,
        assertion_results=[{"kind": "deliverable_nonempty", "ok": False, "length": 0}],
        events_captured=37,
        backend_mode="ephemeral",
        port=53122,
    )
    assert verdict == {
        "scenario": "bargein-demo",
        "ok": False,
        "duration_ms": 1840,
        "assertions": [{"kind": "deliverable_nonempty", "ok": False, "length": 0}],
        "events_captured": 37,
        "backend": {"mode": "ephemeral", "port": 53122},
        "llm": "fake",
    }


# --- timeline dispatch (stub capture, no real backend) -----------------------


class _StubCapture:
    """Records ``wait_for`` calls; answers from a preset queue of booleans."""

    def __init__(self, *, wait_results: list[bool] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._wait_results = list(wait_results or [])
        self.wait_calls: list[int] = []

    async def wait_for(self, predicate: Any, *, timeout_ms: int) -> bool:
        self.wait_calls.append(timeout_ms)
        return self._wait_results.pop(0) if self._wait_results else False


async def test_timeline_inject_text_calls_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    injected: list[tuple[str, str]] = []

    async def _fake_inject(ws_base: str, text: str, **_kw: Any) -> None:
        injected.append((ws_base, text))

    monkeypatch.setattr("bob.attest.runner.inject_text", _fake_inject)

    scenario = Scenario.from_dict(
        {"name": "x", "timeline": [{"do": "inject_text", "text": "hello"}]}
    )
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline("ws://h", _StubCapture(), errors)  # type: ignore[arg-type]

    assert injected == [("ws://h", "hello")]
    assert errors == []


async def test_inject_audio_voiced_forwards_0103_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Issue 0103: a voiced inject_audio step forwards silence_count / frame_gap_ms
    # / await_reply / settle_ms to the drive layer (the semantic-endpoint dials).
    captured: dict[str, Any] = {}

    async def _fake_audio(ws_base: str, frames: list[bytes], **kw: Any) -> None:
        captured["ws_base"] = ws_base
        captured["n_frames"] = len(frames)
        captured.update(kw)

    monkeypatch.setattr("bob.attest.runner.inject_audio_ws", _fake_audio)

    scenario = Scenario.from_dict(
        {
            "name": "x",
            "timeline": [
                {
                    "do": "inject_audio",
                    "transcript": "quel temps fait il",
                    "voiced": True,
                    "thinker": True,
                    "silence_count": 12,
                    "frame_gap_ms": 15,
                    "await_reply": False,
                    "settle_ms": 800,
                }
            ],
        }
    )
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline("ws://h", _StubCapture(), errors)  # type: ignore[arg-type]

    assert errors == []
    assert captured["frame_gap_ms"] == 15
    assert captured["await_reply"] is False
    assert captured["settle_ms"] == 800


async def test_inject_audio_voiced_defaults_preserve_prior_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the 0103 knobs a voiced step still awaits the reply with no pacing —
    # exactly the 0100/0110 behaviour (zero regression).
    captured: dict[str, Any] = {}

    async def _fake_audio(ws_base: str, frames: list[bytes], **kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr("bob.attest.runner.inject_audio_ws", _fake_audio)
    scenario = Scenario.from_dict(
        {
            "name": "x",
            "timeline": [{"do": "inject_audio", "transcript": "hi", "voiced": True}],
        }
    )
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline("ws://h", _StubCapture(), errors)  # type: ignore[arg-type]
    assert errors == []
    assert captured["await_reply"] is True
    assert captured["frame_gap_ms"] == 0


def test_extra_env_pins_thinker_debounce_for_inject_audio() -> None:
    # Issue 0102/0103: a voiced inject_audio carrying ``thinker: true`` pins
    # THINKER_DEBOUNCE_MS=0 so the background Thinker fires on the first partial.
    step = {"do": "inject_audio", "transcript": "hi", "voiced": True, "thinker": True}
    scenario = Scenario.from_dict({"name": "x", "timeline": [step]})
    env = ScenarioRunner(scenario)._extra_env()
    assert env["THINKER_DEBOUNCE_MS"] == "0"


def test_extra_env_scenario_env_merges() -> None:
    # Issue 0103: a scenario-level ``env`` (e.g. a large ENDPOINT_SILENCE_MS to
    # make the silence floor unreachable) is merged into the backend env.
    scenario = Scenario.from_dict(
        {
            "name": "x",
            "env": {"ENDPOINT_SILENCE_MS": "4000"},
            "timeline": [{"do": "inject_audio", "transcript": "hi", "voiced": True}],
        }
    )
    env = ScenarioRunner(scenario)._extra_env()
    assert env["ENDPOINT_SILENCE_MS"] == "4000"


async def test_timeline_wait_event_records_timeout_as_error() -> None:
    scenario = Scenario.from_dict(
        {"name": "x", "timeline": [{"do": "wait_event", "type": "say", "timeout_ms": 5}]}
    )
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    capture = _StubCapture(wait_results=[False])
    await runner._execute_timeline("ws://h", capture, errors)  # type: ignore[arg-type]

    assert capture.wait_calls == [5]
    assert len(errors) == 1
    assert "not observed within 5ms" in errors[0]


async def test_timeline_wait_event_unknown_type_is_loud() -> None:
    # ``backchannel`` is a documented Annexe A.2 logical type not yet wired (its
    # slice is 0105); ``bargein`` is now known (issue 0101). An unknown type must
    # record a loud timeline error rather than passing silently.
    scenario = Scenario.from_dict(
        {"name": "x", "timeline": [{"do": "wait_event", "type": "backchannel"}]}
    )
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline("ws://h", _StubCapture(), errors)  # type: ignore[arg-type]
    assert any("unknown logical event type" in e for e in errors)


async def test_wait_state_op_is_implemented_and_synchronises() -> None:
    # ``inject_audio`` (issue 0099) and ``wait_state`` (issue 0100 — the FSM
    # slice) are both implemented now. ``wait_state`` must synchronise on a
    # ``turn_state`` voice event whose ``to`` matches and NOT record a "not
    # implemented" error (the old stub behaviour). A stub capture that reports
    # the state reached drives the happy path here; the timeout / unknown
    # branches live in test_attest_fsm.
    turn_state_event = {
        "category": "voice",
        "payload": {"ws_event": {"type": "turn_state", "to": "bob_speaking"}},
    }
    scenario = Scenario.from_dict(
        {
            "name": "x",
            "timeline": [{"do": "wait_state", "state": "bob_speaking", "timeout_ms": 50}],
        }
    )
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline(
        "ws://h",
        _StubCapture(wait_results=[True]),  # type: ignore[arg-type]
        errors,
    )
    # No "not implemented" error — the op ran. (The stub answers the predicate
    # via its preset True; the real matcher is unit-tested in test_attest_fsm.)
    assert errors == []
    _ = turn_state_event  # documents the frame shape wait_state matches on


async def test_timeline_unknown_op_is_loud() -> None:
    scenario = Scenario.from_dict({"name": "x", "timeline": [{"do": "teleport"}]})
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline("ws://h", _StubCapture(), errors)  # type: ignore[arg-type]
    assert any("unknown op 'teleport'" in e for e in errors)


async def test_timeline_wait_ms_sleeps_without_error() -> None:
    scenario = Scenario.from_dict({"name": "x", "timeline": [{"do": "wait_ms", "ms": 1}]})
    runner = ScenarioRunner(scenario)
    errors: list[str] = []
    await runner._execute_timeline("ws://h", _StubCapture(), errors)  # type: ignore[arg-type]
    assert errors == []
