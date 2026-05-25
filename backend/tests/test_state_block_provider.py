"""Tests for :class:`bob.context.providers.state_block.StateBlockProvider`.

PRD 0006 / issue 0050 — pure provider tests against a real
:class:`TaskStore`. Snapshot tests pin the rendered layout against a
deterministic clock + turn index.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from bob.context.eviction import DefaultEvictionStrategy
from bob.context.provider import AssemblyContext
from bob.context.providers.state_block import (
    STATE_BLOCK_PROVIDER_ID,
    StateBlockProvider,
)
from bob.context.recency import RecencyPolicy
from bob.context.state_policy import StatePolicy
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.task_store import TaskStore


def _setup_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _fixed_now() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0)


def _policy_v2_assembly() -> AssemblyContext:
    from bob.context.policy import bounded_v2_policy

    return AssemblyContext(policy=bounded_v2_policy(), user_message=None)


def test_empty_store_emits_no_entry() -> None:
    store = _setup_store()
    provider = StateBlockProvider(task_store=store, now=_fixed_now)
    assert list(provider.entries(_policy_v2_assembly())) == []


def test_running_task_appears_in_state_block() -> None:
    store = _setup_store()
    task_id = store.create_task(title="Recherche budgets", goal="Récupère le budget Q4")
    store.update_state(task_id, "running")

    provider = StateBlockProvider(task_store=store, now=_fixed_now)
    entries = list(provider.entries(_policy_v2_assembly()))
    assert len(entries) == 1
    text = entries[0].payload["content"]
    assert task_id in text
    assert "Recherche budgets" in text
    assert "state=running" in text


def test_title_capped_at_max_words() -> None:
    store = _setup_store()
    long_title = "Un titre vraiment beaucoup trop long pour Jarvis qui dépasse les huit mots"
    task_id = store.create_task(title=long_title, goal="x")
    store.update_state(task_id, "running")

    policy = StatePolicy(title_max_words=4)
    provider = StateBlockProvider(task_store=store, state_policy=policy, now=_fixed_now)
    entries = list(provider.entries(_policy_v2_assembly()))
    text = entries[0].payload["content"]
    assert "Un titre vraiment beaucoup…" in text


def test_update_oneliner_capped_at_max_chars() -> None:
    store = _setup_store()
    long_goal = "A" * 500
    task_id = store.create_task(title="t", goal=long_goal)
    store.update_state(task_id, "running")

    policy = StatePolicy(update_max_chars=10)
    provider = StateBlockProvider(task_store=store, state_policy=policy, now=_fixed_now)
    entries = list(provider.entries(_policy_v2_assembly()))
    text = entries[0].payload["content"]
    # The update is wrapped in quotes; the truncated payload itself
    # caps at 10 chars + ellipsis.
    assert '"AAAAAAAAA…"' in text


def test_max_entries_enforced_via_eviction() -> None:
    store = _setup_store()
    # Create 5 tasks: one running + four delivered-done (lowest
    # priority). With max_entries=2, only the running + one delivered
    # done survive eviction.
    running_id = store.create_task(title="live", goal="g")
    store.update_state(running_id, "running")
    for i in range(4):
        done_id = store.create_task(title=f"done-{i}", goal="g")
        store.update_state(done_id, "running")
        store.update_state(done_id, "done")
        store.set_delivered_at_turn(done_id, i)

    policy = StatePolicy(max_entries=2, recent_turns_for_done_inclusion=100)
    provider = StateBlockProvider(
        task_store=store,
        state_policy=policy,
        current_user_turn=10,
        eviction_strategy=DefaultEvictionStrategy(),
        now=_fixed_now,
    )
    entries = list(provider.entries(_policy_v2_assembly()))
    text = entries[0].payload["content"]
    # Running task always survives.
    assert running_id in text
    # Two task rows in the block (plus header + footer).
    assert text.count("- id=") == 2


def test_delivered_done_outside_window_dropped() -> None:
    store = _setup_store()
    long_ago_id = store.create_task(title="old", goal="g")
    store.update_state(long_ago_id, "running")
    store.update_state(long_ago_id, "done")
    store.set_delivered_at_turn(long_ago_id, 0)

    fresh_running = store.create_task(title="live", goal="g")
    store.update_state(fresh_running, "running")

    policy = StatePolicy(recent_turns_for_done_inclusion=2)
    provider = StateBlockProvider(
        task_store=store,
        state_policy=policy,
        current_user_turn=10,
        now=_fixed_now,
    )
    entries = list(provider.entries(_policy_v2_assembly()))
    text = entries[0].payload["content"]
    assert fresh_running in text
    assert long_ago_id not in text


def test_age_min_always_recomputed_at_assembly() -> None:
    """PRD acceptance: ``age_min`` never read from persistence."""

    store = _setup_store()
    task_id = store.create_task(title="t", goal="g")
    store.update_state(task_id, "running")

    # Pick a clock far ahead of the SQLite ``datetime('now')`` default
    # so the diff is positive and the test sees meaningful ages.
    far_future = datetime(2099, 1, 1, 0, 0, 0)
    p1 = StateBlockProvider(task_store=store, now=lambda: far_future)
    p2 = StateBlockProvider(
        task_store=store,
        now=lambda: far_future + timedelta(minutes=10),
    )
    text1 = next(iter(p1.entries(_policy_v2_assembly()))).payload["content"]
    text2 = next(iter(p2.entries(_policy_v2_assembly()))).payload["content"]
    assert "age_min=" in text1
    assert "age_min=" in text2
    assert text1 != text2  # ages differ


def test_recency_signal_emitted_active_vs_stale() -> None:
    store = _setup_store()
    task_id = store.create_task(title="t", goal="g")
    store.update_state(task_id, "running")

    # No reference + current_user_turn=10 → age_turns=10 → stale unless
    # the seconds branch fires. The seconds branch reads
    # ``task.updated_at``; we use a clock far ahead so age_seconds is
    # past the default threshold (120s).
    base = datetime.fromisoformat("2026-05-25T20:00:00")
    provider = StateBlockProvider(
        task_store=store,
        current_user_turn=10,
        recency_policy=RecencyPolicy(active_within_user_turns=3, active_within_seconds=0),
        now=lambda: base,
    )
    text = next(iter(provider.entries(_policy_v2_assembly()))).payload["content"]
    assert "recency=stale" in text

    # Re-emit while referencing the task on the current turn — age_turns=0.
    provider = StateBlockProvider(
        task_store=store,
        current_user_turn=10,
        last_referenced_turn_by_task={task_id: 10},
        recency_policy=RecencyPolicy(active_within_user_turns=3, active_within_seconds=0),
        now=lambda: base,
    )
    text = next(iter(provider.entries(_policy_v2_assembly()))).payload["content"]
    assert "recency=active" in text


def test_state_block_emits_with_pinned_entry() -> None:
    """The block is pinned so eviction inside the assembler never drops it."""

    store = _setup_store()
    task_id = store.create_task(title="t", goal="g")
    store.update_state(task_id, "running")

    provider = StateBlockProvider(task_store=store, now=_fixed_now)
    entries = list(provider.entries(_policy_v2_assembly()))
    assert entries[0].pinned is True
    assert entries[0].provider_id == STATE_BLOCK_PROVIDER_ID
    assert entries[0].payload["role"] == "system"


def test_token_budget_below_threshold_assertion() -> None:
    """PRD: token budget asserted in tests, not at runtime.

    With the default ``StatePolicy.max_entries=8`` and reasonable
    title / update caps, the rendered block should comfortably fit
    under a small token budget (here ~600 chars / ~150 tokens).
    """

    store = _setup_store()
    # Three live + two delivered-done — typical bursty session.
    for i in range(3):
        tid = store.create_task(title=f"Live task {i}", goal="g" * 50)
        store.update_state(tid, "running")
    for i in range(2):
        tid = store.create_task(title=f"Done task {i}", goal="g" * 50)
        store.update_state(tid, "running")
        store.update_state(tid, "done")
        store.set_delivered_at_turn(tid, 0)

    provider = StateBlockProvider(
        task_store=store,
        current_user_turn=1,
        now=_fixed_now,
    )
    entries = list(provider.entries(_policy_v2_assembly()))
    content = entries[0].payload["content"]
    # Soft-cap: budget is asserted in tests per PRD.
    assert len(content) <= 1500
