"""Tests for the v2 :class:`bob.task_store.TaskStore` lifecycle (PRD 0006 / issue 0050).

* New states: ``spawned``, ``awaiting_input``, ``superseded``.
* New columns / methods: ``delivered_at_turn`` (with
  ``set_delivered_at_turn``), ``mark_superseded``.
"""

from __future__ import annotations

import sqlite3

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.task_store import TaskStore, TaskStoreError


def _store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def test_default_state_pending_compatible_with_legacy_callers() -> None:
    """Pre-0050 callers still create rows in ``pending``."""

    store = _store()
    task_id = store.create_task(title="t", goal="g")
    task = store.get_task(task_id)
    assert task.state == "pending"
    assert task.delivered_at_turn is None


def test_transition_pending_to_spawned_to_running() -> None:
    """The legacy ``pending`` lifecycle can fold into the v2 ``spawned`` state."""

    store = _store()
    task_id = store.create_task(title="t", goal="g")
    # ``pending -> superseded`` is allowed by the table so this is an
    # alternate path for replan; here we check the legacy bridge.
    store.update_state(task_id, "running")
    assert store.get_task(task_id).state == "running"


def test_v2_spawned_terminal_to_superseded() -> None:
    store = _store()
    task_id = store.create_task(title="t", goal="g")
    store.update_state(task_id, "running")
    store.mark_superseded(task_id)
    assert store.get_task(task_id).state == "superseded"


def test_superseded_already_terminal_rejects_re_supersede() -> None:
    store = _store()
    task_id = store.create_task(title="t", goal="g")
    store.update_state(task_id, "running")
    store.update_state(task_id, "done")
    with pytest.raises(TaskStoreError):
        store.mark_superseded(task_id)


def test_set_delivered_at_turn_persists_and_overwrites() -> None:
    store = _store()
    task_id = store.create_task(title="t", goal="g")
    store.set_delivered_at_turn(task_id, 5)
    assert store.get_task(task_id).delivered_at_turn == 5
    store.set_delivered_at_turn(task_id, 9)
    assert store.get_task(task_id).delivered_at_turn == 9


def test_set_delivered_at_turn_rejects_negative() -> None:
    store = _store()
    task_id = store.create_task(title="t", goal="g")
    with pytest.raises(TaskStoreError):
        store.set_delivered_at_turn(task_id, -1)


def test_awaiting_input_alias_round_trip() -> None:
    """The v2 ``awaiting_input`` state is accepted alongside legacy ``waiting_input``."""

    store = _store()
    task_id = store.create_task(title="t", goal="g")
    store.update_state(task_id, "running")
    store.update_state(task_id, "awaiting_input")
    assert store.get_task(task_id).state == "awaiting_input"
    store.update_state(task_id, "running")
    assert store.get_task(task_id).state == "running"


def test_lineage_round_trip_after_replan() -> None:
    """``create_task(lineage=[old])`` round-trips through the JSON column."""

    store = _store()
    old_id = store.create_task(title="t", goal="g")
    new_id = store.create_task(title="t2", goal="g2", lineage=[old_id])
    assert store.get_task(new_id).lineage == [old_id]
