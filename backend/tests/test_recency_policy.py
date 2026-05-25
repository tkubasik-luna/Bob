"""Pure tests for :class:`bob.context.recency.RecencyPolicy` (PRD 0006 / issue 0050).

The policy is a deep, pure module: given a :class:`RecencySignal` + a
:class:`RecencyPolicy` it returns either ``"active"`` or ``"stale"``.
Tests pin the determinism guarantee + every documented branch.
"""

from __future__ import annotations

import pytest

from bob.context.recency import (
    RecencyPolicy,
    RecencySignal,
    classify_recency,
    default_recency_policy,
)


def test_active_within_turn_window() -> None:
    policy = RecencyPolicy(active_within_user_turns=3, active_within_seconds=0)
    assert (
        classify_recency(RecencySignal(age_turns=0, age_seconds=999_999), policy=policy) == "active"
    )
    assert (
        classify_recency(RecencySignal(age_turns=3, age_seconds=999_999), policy=policy) == "active"
    )


def test_stale_past_turn_window_without_seconds_signal() -> None:
    policy = RecencyPolicy(active_within_user_turns=3, active_within_seconds=0)
    assert (
        classify_recency(RecencySignal(age_turns=4, age_seconds=999_999), policy=policy) == "stale"
    )


def test_active_within_seconds_overrides_turn_window() -> None:
    """The seconds branch is independent: a long age_turns still classifies active.

    Mirrors the PRD's "if recently *touched* on the clock, it's still
    fresh in the user's mind even if many other turns have passed".
    """

    policy = RecencyPolicy(active_within_user_turns=2, active_within_seconds=60)
    signal = RecencySignal(age_turns=10, age_seconds=30)
    assert classify_recency(signal, policy=policy) == "active"


def test_topic_overlap_zero_disabled_by_default() -> None:
    """``topic_overlap_min=0`` means "ignore overlap" — never fires on its own."""

    policy = default_recency_policy()
    signal = RecencySignal(age_turns=999, age_seconds=999, topic_overlap=1.0)
    assert classify_recency(signal, policy=policy) == "stale"


def test_topic_overlap_signal_fires_when_threshold_above_zero() -> None:
    policy = RecencyPolicy(
        active_within_user_turns=0,
        active_within_seconds=0,
        topic_overlap_min=0.5,
    )
    assert (
        classify_recency(
            RecencySignal(age_turns=10, age_seconds=999, topic_overlap=0.6),
            policy=policy,
        )
        == "active"
    )
    assert (
        classify_recency(
            RecencySignal(age_turns=10, age_seconds=999, topic_overlap=0.49),
            policy=policy,
        )
        == "stale"
    )


def test_recency_policy_is_deterministic() -> None:
    """PRD acceptance criterion: identical inputs → identical decision."""

    policy = default_recency_policy()
    signal = RecencySignal(age_turns=2, age_seconds=45, topic_overlap=0.2)
    decisions = {classify_recency(signal, policy=policy) for _ in range(50)}
    assert decisions == {"active"}


def test_invalid_policy_fields_rejected() -> None:
    with pytest.raises(ValueError):
        RecencyPolicy(active_within_user_turns=-1)
    with pytest.raises(ValueError):
        RecencyPolicy(active_within_seconds=-1)
    with pytest.raises(ValueError):
        RecencyPolicy(topic_overlap_min=2.0)
