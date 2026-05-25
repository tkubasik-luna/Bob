"""Unit tests for :mod:`bob.validation.envelope`."""

from __future__ import annotations

from bob.validation.envelope import CallEnvelope


def test_envelope_starts_at_attempt_one() -> None:
    env = CallEnvelope(tool_name="say", actor="jarvis")
    assert env.attempts == 1
    assert env.retries_used == 0
    assert env.feedback == []


def test_increment_bumps_attempts() -> None:
    env = CallEnvelope(tool_name="say", actor="jarvis")
    env.increment(error_code="invalid_args")
    assert env.attempts == 2
    assert env.retries_used == 1
    assert env.last_error_code == "invalid_args"


def test_record_feedback_appends_lines() -> None:
    env = CallEnvelope(tool_name=None, actor="sub_agent")
    env.record_feedback("[INVALID OUTPUT]: missing required field")
    env.record_feedback("[INVALID OUTPUT]: still missing field")
    assert env.feedback == [
        "[INVALID OUTPUT]: missing required field",
        "[INVALID OUTPUT]: still missing field",
    ]
