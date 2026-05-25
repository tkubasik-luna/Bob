"""Tests for :mod:`bob.context.policy`."""

from __future__ import annotations

from bob.context.policy import (
    LEGACY_FULL_HISTORY_POLICY_ID,
    legacy_full_history_policy,
    parse_policy_overrides,
)


def test_default_policy_id_constant() -> None:
    assert LEGACY_FULL_HISTORY_POLICY_ID == "legacy_full_history"


def test_legacy_full_history_policy_uses_single_provider() -> None:
    policy = legacy_full_history_policy()

    assert policy.policy_id == LEGACY_FULL_HISTORY_POLICY_ID
    assert tuple(policy.provider_ids) == ("legacy_full_history",)


def test_legacy_full_history_policy_defers_budget_fields_to_none() -> None:
    policy = legacy_full_history_policy()

    assert policy.token_budget is None
    assert policy.recent_turns_window is None
    assert policy.state_cap is None
    assert policy.eviction_policy_id is None


def test_parse_policy_overrides_falls_back_to_defaults() -> None:
    policy = parse_policy_overrides()

    assert policy == legacy_full_history_policy()


def test_parse_policy_overrides_applies_each_field() -> None:
    policy = parse_policy_overrides(
        policy_id="custom",
        provider_ids=["a", "b"],
        token_budget=8000,
        recent_turns_window=3,
        state_cap=8,
        eviction_policy_id="recency",
    )

    assert policy.policy_id == "custom"
    assert tuple(policy.provider_ids) == ("a", "b")
    assert policy.token_budget == 8000
    assert policy.recent_turns_window == 3
    assert policy.state_cap == 8
    assert policy.eviction_policy_id == "recency"


def test_context_policy_is_frozen() -> None:
    policy = legacy_full_history_policy()
    try:
        policy.policy_id = "other"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ContextPolicy must be frozen")


def test_parse_policy_overrides_only_partial_override() -> None:
    """Partial overrides leave untouched fields at their defaults."""

    policy = parse_policy_overrides(token_budget=4096)

    default = legacy_full_history_policy()
    assert policy.token_budget == 4096
    assert policy.policy_id == default.policy_id
    assert policy.provider_ids == default.provider_ids
    assert policy.recent_turns_window is None


def test_parse_policy_overrides_normalises_provider_ids_to_tuple() -> None:
    """The dataclass stores ``provider_ids`` as a tuple regardless of input shape."""

    policy = parse_policy_overrides(provider_ids=["x", "y"])
    assert isinstance(policy.provider_ids, tuple)
