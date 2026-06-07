"""Unit tests for the attestation assertion engine (issue 0098).

Each implemented kind is exercised on both a passing and a failing input, plus
the registry's loud-fail behaviour for an unknown kind and the deliverable
projection rule.
"""

from __future__ import annotations

from typing import Any

from bob.attest.assertions import (
    AssertionContext,
    known_kinds,
    project_deliverable,
    register_assertion,
    run_assertion,
)


def _say_event(speech: str) -> dict[str, Any]:
    """A captured ``output`` debug frame carrying a spoken reply (a ``say``)."""

    return {
        "category": "output",
        "severity": "info",
        "source": "orchestrator.process_user_message",
        "summary": f'Bob répond: "{speech[:80]}"',
        "payload": {"speech": speech, "ui": []},
    }


def _error_event(summary: str) -> dict[str, Any]:
    return {
        "category": "system",
        "severity": "error",
        "source": "bob.ws_router.chat_ws",
        "summary": summary,
        "payload": {},
    }


def _ctx(events: list[dict[str, Any]]) -> AssertionContext:
    return AssertionContext(events=events, deliverable=project_deliverable(events))


# --- event_emitted -----------------------------------------------------------


def test_event_emitted_passes_when_say_present() -> None:
    ctx = _ctx([_say_event("bonjour")])
    result = run_assertion({"kind": "event_emitted", "type": "say"}, ctx)
    assert result.ok is True
    assert result.to_dict() == {"kind": "event_emitted", "ok": True, "type": "say", "matched": 1}


def test_event_emitted_fails_when_say_absent() -> None:
    ctx = _ctx([])
    result = run_assertion({"kind": "event_emitted", "type": "say"}, ctx)
    assert result.ok is False
    assert result.detail["matched"] == 0


def test_event_emitted_unknown_logical_type_fails_loudly() -> None:
    ctx = _ctx([_say_event("hi")])
    result = run_assertion({"kind": "event_emitted", "type": "bargein"}, ctx)
    assert result.ok is False
    assert "unknown logical event type" in result.detail["error"]


def test_event_emitted_missing_type_fails() -> None:
    result = run_assertion({"kind": "event_emitted"}, _ctx([]))
    assert result.ok is False
    assert "requires a 'type'" in result.detail["error"]


def test_event_emitted_ignores_output_event_with_blank_speech() -> None:
    blank = _say_event("")
    blank["payload"]["speech"] = "   "
    result = run_assertion({"kind": "event_emitted", "type": "say"}, _ctx([blank]))
    assert result.ok is False


# --- no_error_events ---------------------------------------------------------


def test_no_error_events_passes_with_only_info() -> None:
    ctx = _ctx([_say_event("ok")])
    result = run_assertion({"kind": "no_error_events"}, ctx)
    assert result.ok is True
    assert result.detail["error_count"] == 0


def test_no_error_events_fails_when_error_present() -> None:
    ctx = _ctx([_say_event("ok"), _error_event("LLM injoignable pendant le turn")])
    result = run_assertion({"kind": "no_error_events"}, ctx)
    assert result.ok is False
    assert result.detail["error_count"] == 1
    assert result.detail["errors"][0]["summary"] == "LLM injoignable pendant le turn"


# --- deliverable_nonempty ----------------------------------------------------


def test_deliverable_nonempty_passes_with_spoken_reply() -> None:
    ctx = _ctx([_say_event("voici la réponse")])
    result = run_assertion({"kind": "deliverable_nonempty"}, ctx)
    assert result.ok is True
    assert result.detail["length"] == len("voici la réponse")


def test_deliverable_nonempty_fails_with_no_reply() -> None:
    ctx = _ctx([])
    result = run_assertion({"kind": "deliverable_nonempty"}, ctx)
    assert result.ok is False
    assert result.detail["length"] == 0


# --- deliverable projection --------------------------------------------------


def test_project_deliverable_takes_last_nonempty_say() -> None:
    events = [_say_event("premier"), _say_event("dernier")]
    assert project_deliverable(events) == "dernier"


def test_project_deliverable_empty_when_no_say() -> None:
    assert project_deliverable([_error_event("boom")]) == ""


# --- registry ----------------------------------------------------------------


def test_unknown_kind_is_loud_fail_not_silent_pass() -> None:
    result = run_assertion({"kind": "fsm_reached", "state": "bob_speaking"}, _ctx([]))
    assert result.ok is False
    assert "not implemented yet" in result.detail["error"]
    assert "fsm_reached" in result.detail["error"]


def test_missing_kind_fails() -> None:
    result = run_assertion({"state": "x"}, _ctx([]))
    assert result.ok is False
    assert result.kind == "<missing>"


def test_register_assertion_extends_the_engine() -> None:
    # The extensibility seam: a later slice registers a new kind and dispatch
    # picks it up with no other change.
    register_assertion(
        "always_true_probe",
        lambda spec, ctx: __import__(
            "bob.attest.assertions", fromlist=["AssertionResult"]
        ).AssertionResult(kind="always_true_probe", ok=True),
    )
    try:
        assert "always_true_probe" in known_kinds()
        result = run_assertion({"kind": "always_true_probe"}, _ctx([]))
        assert result.ok is True
    finally:
        # Keep module-level registry clean for other tests.
        from bob.attest import assertions as _assertions

        _assertions._REGISTRY.pop("always_true_probe", None)
