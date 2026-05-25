"""Pure tests for :class:`bob.context.eviction.DefaultEvictionStrategy`.

PRD 0006 / issue 0050. The strategy is a pure sort over candidate
rows; tests pin the documented order (oldest delivered-done → failed
→ awaiting → never running) without going through SQLite.
"""

from __future__ import annotations

import pytest

from bob.context.eviction import (
    DefaultEvictionStrategy,
    StateBlockCandidate,
)


def _c(
    task_id: str,
    state: str,
    *,
    delivered_at_turn: int | None = None,
    order: int = 0,
) -> StateBlockCandidate:
    return StateBlockCandidate(
        task_id=task_id,
        state=state,
        delivered_at_turn=delivered_at_turn,
        order_key=(order, 0),
    )


def test_delivered_done_evicted_before_failed() -> None:
    """Delivered ``done`` rows leave first; ``failed`` rows leave next."""

    strategy = DefaultEvictionStrategy()
    candidates = [
        _c("t-done", "done", delivered_at_turn=1, order=1),
        _c("t-failed", "failed", order=2),
        _c("t-running", "running", order=3),
    ]
    survivors = strategy.evict_to_cap(candidates, cap=2)
    survivor_ids = {c.task_id for c in survivors}
    assert "t-running" in survivor_ids
    assert "t-failed" in survivor_ids
    assert "t-done" not in survivor_ids


def test_running_never_evicted_even_when_cap_is_tight() -> None:
    """A cap of ``1`` against 3 candidates still preserves ``running``."""

    strategy = DefaultEvictionStrategy()
    candidates = [
        _c("t-running", "running", order=0),
        _c("t-failed-1", "failed", order=1),
        _c("t-failed-2", "failed", order=2),
    ]
    survivors = strategy.evict_to_cap(candidates, cap=1)
    assert any(c.task_id == "t-running" for c in survivors)


def test_awaiting_input_evicted_before_running() -> None:
    strategy = DefaultEvictionStrategy()
    candidates = [
        _c("t-await", "awaiting_input", order=0),
        _c("t-running", "running", order=1),
    ]
    survivors = strategy.evict_to_cap(candidates, cap=1)
    assert [c.task_id for c in survivors] == ["t-running"]


def test_undelivered_done_kept_ahead_of_delivered_done() -> None:
    """A ``done`` row without ``delivered_at_turn`` outranks a delivered one."""

    strategy = DefaultEvictionStrategy()
    candidates = [
        _c("t-pending-delivery", "done", delivered_at_turn=None, order=0),
        _c("t-delivered", "done", delivered_at_turn=5, order=1),
        _c("t-running", "running", order=2),
    ]
    survivors = strategy.evict_to_cap(candidates, cap=2)
    survivor_ids = [c.task_id for c in survivors]
    assert "t-delivered" not in survivor_ids
    assert "t-pending-delivery" in survivor_ids
    assert "t-running" in survivor_ids


def test_eviction_preserves_original_order_on_survivors() -> None:
    strategy = DefaultEvictionStrategy()
    candidates = [
        _c("a-running", "running", order=0),
        _c("b-await", "awaiting_input", order=1),
        _c("c-running", "running", order=2),
    ]
    survivors = strategy.evict_to_cap(candidates, cap=2)
    assert [c.task_id for c in survivors] == ["a-running", "c-running"]


def test_no_eviction_under_cap() -> None:
    strategy = DefaultEvictionStrategy()
    candidates = [_c("t", "running")]
    survivors = strategy.evict_to_cap(candidates, cap=5)
    assert survivors == candidates


def test_negative_cap_rejected() -> None:
    strategy = DefaultEvictionStrategy()
    with pytest.raises(ValueError):
        strategy.evict_to_cap([], cap=-1)


def test_mixed_states_full_eviction_order() -> None:
    """Cover every bucket in one sort to pin the documented order."""

    strategy = DefaultEvictionStrategy()
    candidates = [
        _c("running-A", "running", order=0),
        _c("done-delivered", "done", delivered_at_turn=2, order=1),
        _c("failed", "failed", order=2),
        _c("awaiting", "awaiting_input", order=3),
        _c("spawned", "spawned", order=4),
        _c("running-B", "running", order=5),
        _c("superseded", "superseded", delivered_at_turn=4, order=6),
    ]
    survivors = strategy.evict_to_cap(candidates, cap=2)
    survivor_ids = {c.task_id for c in survivors}
    # The two ``running`` rows must survive.
    assert survivor_ids == {"running-A", "running-B"}
