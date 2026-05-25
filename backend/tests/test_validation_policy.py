"""Unit tests for :mod:`bob.validation.policy`."""

from __future__ import annotations

from bob.validation.policy import (
    DEFAULT_POLICY,
    POLICY_TABLE,
    SUB_AGENT_DEFAULT_POLICY,
    RetryPolicy,
    get_policy,
)


def test_default_policy_shape() -> None:
    assert DEFAULT_POLICY.max_retries >= 0
    assert DEFAULT_POLICY.degrade_action == "hardcoded_say"
    assert DEFAULT_POLICY.accept_partial is False


def test_sub_agent_default_policy_uses_forced_done_failed() -> None:
    assert SUB_AGENT_DEFAULT_POLICY.degrade_action == "forced_done_failed"


def test_get_policy_returns_default_for_unknown_tool() -> None:
    """Unknown tool name falls back to :data:`DEFAULT_POLICY`."""

    assert get_policy("never_registered_tool") is DEFAULT_POLICY


def test_get_policy_returns_registered_entry() -> None:
    """Known tool name resolves to its own row."""

    say_policy = get_policy("say")
    assert say_policy is POLICY_TABLE["say"]
    assert say_policy.accept_partial is True


def test_retry_policy_fields_are_typed() -> None:
    """Type sanity — the dataclass holds the right shapes."""

    policy = RetryPolicy(max_retries=2, degrade_action="hardcoded_say", accept_partial=False)
    assert policy.max_retries == 2
    assert policy.degrade_action == "hardcoded_say"
    assert policy.accept_partial is False
