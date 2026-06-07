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
    # ``backchannel`` is a documented Annexe A.2 logical type whose matcher has
    # NOT been wired yet (its slice is 0105) — referencing it must FAIL loudly.
    # (``bargein`` landed in issue 0101 and is exercised in test_attest_bargein.)
    ctx = _ctx([_say_event("hi")])
    result = run_assertion({"kind": "event_emitted", "type": "backchannel"}, ctx)
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
    # ``draft_hit`` is a documented Annexe F derived whose assertion kind has NOT
    # been implemented yet (its slice is the speculative Draft, 0104) —
    # referencing it must FAIL loudly, naming the kind, never silently pass.
    # (``latency_lt_ms`` / ``transcript_roundtrip_similarity_gte`` landed in
    # issue 0110 and are exercised below; ``bargein_within_ms`` /
    # ``committed_equals_spoken`` landed in 0101, see test_attest_bargein.)
    result = run_assertion({"kind": "draft_hit", "expected": True}, _ctx([]))
    assert result.ok is False
    assert "not implemented yet" in result.detail["error"]
    assert "draft_hit" in result.detail["error"]


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


# --- latency_lt_ms (issue 0110, Annexe C + F) --------------------------------


def _turn_latency_event(
    marks: dict[str, float], derived: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A captured ``turn_latency`` voice frame (nested under payload.ws_event)."""

    return {
        "category": "voice",
        "severity": "debug",
        "source": "bob.voice_loop.turn_latency",
        "summary": "turn_latency",
        "payload": {
            "ws_event": {
                "type": "turn_latency",
                "turn_id": "t1",
                "marks": marks,
                "derived": derived or {},
            }
        },
    }


def test_latency_lt_ms_passes_under_target() -> None:
    # endpoint -> first audio = 500 ms, under the 800 ms committed target.
    ev = _turn_latency_event({"t_endpoint": 10.0, "t_first_audio_chunk": 10.5})
    result = run_assertion(
        {
            "kind": "latency_lt_ms",
            "from_mark": "t_endpoint",
            "to_mark": "t_first_audio_chunk",
            "max": 800,
        },
        _ctx([ev]),
    )
    assert result.ok is True
    assert result.detail["actual"] == 500.0


def test_latency_lt_ms_fails_over_target() -> None:
    ev = _turn_latency_event({"t_endpoint": 10.0, "t_first_audio_chunk": 11.0})
    result = run_assertion(
        {
            "kind": "latency_lt_ms",
            "from_mark": "t_endpoint",
            "to_mark": "t_first_audio_chunk",
            "max": 800,
        },
        _ctx([ev]),
    )
    assert result.ok is False
    assert result.detail["actual"] == 1000.0


def test_latency_lt_ms_takes_best_across_turns() -> None:
    # Two turns; one slow (1000 ms), one fast (300 ms). The best meets max:800.
    slow = _turn_latency_event({"t_endpoint": 10.0, "t_first_audio_chunk": 11.0})
    fast = _turn_latency_event({"t_endpoint": 20.0, "t_first_audio_chunk": 20.3})
    result = run_assertion(
        {
            "kind": "latency_lt_ms",
            "from_mark": "t_endpoint",
            "to_mark": "t_first_audio_chunk",
            "max": 800,
        },
        _ctx([slow, fast]),
    )
    assert result.ok is True
    assert result.detail["actual"] == 300.0


def test_latency_lt_ms_fails_loudly_when_no_turn_latency_event() -> None:
    result = run_assertion(
        {
            "kind": "latency_lt_ms",
            "from_mark": "t_endpoint",
            "to_mark": "t_first_audio_chunk",
            "max": 800,
        },
        _ctx([_say_event("hi")]),
    )
    assert result.ok is False
    assert "no turn_latency event captured" in result.detail["error"]


def test_latency_lt_ms_fails_loudly_when_marks_absent() -> None:
    # The turn finished but never produced t_first_audio_chunk (no audio): the
    # measured span never occurred → loud fail, not a silent pass.
    ev = _turn_latency_event({"t_first_mic_frame": 9.0, "t_endpoint": 10.0})
    result = run_assertion(
        {
            "kind": "latency_lt_ms",
            "from_mark": "t_endpoint",
            "to_mark": "t_first_audio_chunk",
            "max": 800,
        },
        _ctx([ev]),
    )
    assert result.ok is False
    assert "no turn carried both marks" in result.detail["error"]
    assert result.detail["marks_seen"]


def test_latency_lt_ms_requires_marks_and_max() -> None:
    ev = _turn_latency_event({"t_endpoint": 10.0, "t_first_audio_chunk": 10.5})
    missing_from = run_assertion(
        {"kind": "latency_lt_ms", "to_mark": "t_first_audio_chunk", "max": 800}, _ctx([ev])
    )
    assert missing_from.ok is False
    assert "from_mark" in missing_from.detail["error"]
    missing_max = run_assertion(
        {"kind": "latency_lt_ms", "from_mark": "t_endpoint", "to_mark": "t_first_audio_chunk"},
        _ctx([ev]),
    )
    assert missing_max.ok is False
    assert "max" in missing_max.detail["error"]


# --- bargein_within_ms on the bargein event (issue 0101, re-verified) --------


def _bargein_event(detected_ts: float, cut_ts: float, committed: str = "Bonjour") -> dict[str, Any]:
    return {
        "category": "voice",
        "severity": "info",
        "source": "bob.voice_loop.bargein",
        "summary": "bargein",
        "payload": {
            "ws_event": {
                "type": "bargein",
                "turn_id": "t1",
                "detected_ts": detected_ts,
                "cut_ts": cut_ts,
                "committed_spoken_text": committed,
            }
        },
    }


def test_bargein_within_ms_passes_under_target() -> None:
    # cut - detected = 250 ms, equals the Annexe F bargein_cut_ms derived.
    ev = _bargein_event(detected_ts=5.0, cut_ts=5.25)
    result = run_assertion({"kind": "bargein_within_ms", "max": 300}, _ctx([ev]))
    assert result.ok is True
    assert result.detail["actual"] == 250.0


def test_bargein_within_ms_fails_when_no_bargein_event() -> None:
    result = run_assertion({"kind": "bargein_within_ms", "max": 300}, _ctx([_say_event("hi")]))
    assert result.ok is False
    assert "no bargein event captured" in result.detail["error"]


# --- transcript_roundtrip_similarity_gte (issue 0110, --deep) ----------------


def _roundtrip_event(said: str, heard: str, similarity: float | None = None) -> dict[str, Any]:
    ws_event: dict[str, Any] = {"type": "roundtrip_transcript", "said": said, "heard": heard}
    if similarity is not None:
        ws_event["similarity"] = similarity
    return {
        "category": "voice",
        "severity": "debug",
        "source": "bob.attest.roundtrip",
        "summary": "roundtrip_transcript",
        "payload": {"ws_event": ws_event},
    }


def test_roundtrip_similarity_passes_on_exact_match() -> None:
    ev = _roundtrip_event(said="Bonjour je suis Bob", heard="Bonjour je suis Bob")
    result = run_assertion({"kind": "transcript_roundtrip_similarity_gte", "min": 0.95}, _ctx([ev]))
    assert result.ok is True
    assert result.detail["similarity"] == 1.0


def test_roundtrip_similarity_prefers_precomputed_value() -> None:
    # A carried similarity is trusted (the harness computed it); here a perfect
    # carried value passes even though the strings differ.
    ev = _roundtrip_event(said="Bonjour", heard="totally different", similarity=1.0)
    result = run_assertion({"kind": "transcript_roundtrip_similarity_gte", "min": 0.9}, _ctx([ev]))
    assert result.ok is True


def test_roundtrip_similarity_fails_below_min() -> None:
    ev = _roundtrip_event(said="Bonjour je suis Bob", heard="xyz")
    result = run_assertion({"kind": "transcript_roundtrip_similarity_gte", "min": 0.6}, _ctx([ev]))
    assert result.ok is False
    assert result.detail["similarity"] < 0.6


def test_roundtrip_similarity_fails_loudly_without_deep_event() -> None:
    # No roundtrip_transcript captured (i.e. --deep not enabled) → loud fail,
    # never a silent green on a deep assertion.
    result = run_assertion(
        {"kind": "transcript_roundtrip_similarity_gte", "min": 0.6}, _ctx([_say_event("hi")])
    )
    assert result.ok is False
    assert "no roundtrip_transcript event captured" in result.detail["error"]
