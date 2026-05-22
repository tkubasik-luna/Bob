"""Tests for :mod:`bob.jarvis_store`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore


def _make_store_in_memory() -> tuple[JarvisStore, sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return JarvisStore(conn), conn


def test_append_and_history_returns_ordered() -> None:
    store, _conn = _make_store_in_memory()
    store.append("user", "salut")
    store.append("assistant", "yes")
    store.append("user", "ok")

    assert store.history() == [
        {"role": "user", "content": "salut"},
        {"role": "assistant", "content": "yes"},
        {"role": "user", "content": "ok"},
    ]


def test_history_includes_action_only_when_set() -> None:
    store, _conn = _make_store_in_memory()
    store.append("user", "a")
    store.append("assistant", "b", action="done")
    store.append("assistant", "c")

    history = store.history()
    assert history[0] == {"role": "user", "content": "a"}
    assert history[1] == {"role": "assistant", "content": "b", "action": "done"}
    assert history[2] == {"role": "assistant", "content": "c"}
    assert "action" not in history[2]


def test_clear_empties_history() -> None:
    store, _conn = _make_store_in_memory()
    store.append("user", "x")
    store.append("assistant", "y")

    store.clear()

    assert store.history() == []


def test_history_empty_when_no_messages_appended() -> None:
    store, _conn = _make_store_in_memory()
    assert store.history() == []


def test_state_survives_reopen(tmp_path: Path) -> None:
    """A new :class:`JarvisStore` on the same DB file sees prior data."""

    db_path = tmp_path / "bob.db"

    first_conn = sqlite3.connect(db_path)
    apply_migrations(first_conn, default_migrations_dir())
    first = JarvisStore(first_conn)
    first.append("user", "persisted-q")
    first.append("assistant", "persisted-a")
    first_conn.close()

    second_conn = sqlite3.connect(db_path)
    apply_migrations(second_conn, default_migrations_dir())
    second = JarvisStore(second_conn)

    assert second.history() == [
        {"role": "user", "content": "persisted-q"},
        {"role": "assistant", "content": "persisted-a"},
    ]


def test_get_default_store_raises_before_priming() -> None:
    """Accessing the singleton before boot raises a clear error."""

    from bob import jarvis_store as jarvis_store_module

    # Make sure no prior test left one installed.
    previous = None
    try:
        previous = jarvis_store_module.get_default_store()
    except RuntimeError:
        previous = None

    jarvis_store_module.set_default_store(None)
    try:
        with pytest.raises(RuntimeError):
            jarvis_store_module.get_default_store()
    finally:
        jarvis_store_module.set_default_store(previous)


def test_role_check_constraint_rejects_unknown_role() -> None:
    """The SQL CHECK constraint must reject roles outside the allowed set."""

    _store, conn = _make_store_in_memory()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jarvis_messages(role, content) VALUES (?, ?)",
            ("garbage", "x"),
        )
