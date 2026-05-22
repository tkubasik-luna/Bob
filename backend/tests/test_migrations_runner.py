"""Tests for :mod:`bob.db.migrations_runner`."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bob.db.migrations_runner import (
    apply_migrations,
    default_migrations_dir,
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cursor = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cursor.fetchone() is not None


def test_apply_creates_bookkeeping_and_target_tables() -> None:
    conn = sqlite3.connect(":memory:")
    applied = apply_migrations(conn, default_migrations_dir())

    assert "0001_jarvis_messages.sql" in applied
    assert _table_exists(conn, "_migrations")
    assert _table_exists(conn, "jarvis_messages")


def test_apply_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    second_applied = apply_migrations(conn, default_migrations_dir())

    assert second_applied == []
    # First run still recorded.
    rows = conn.execute("SELECT filename FROM _migrations").fetchall()
    assert ("0001_jarvis_messages.sql",) in rows


def test_apply_processes_files_in_lexical_order(tmp_path: Path) -> None:
    """The runner must apply ``*.sql`` files sorted by filename."""

    (tmp_path / "0002_second.sql").write_text(
        "CREATE TABLE b (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (tmp_path / "0001_first.sql").write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    conn = sqlite3.connect(":memory:")
    applied = apply_migrations(conn, tmp_path)

    assert applied == ["0001_first.sql", "0002_second.sql"]


def test_apply_skips_already_applied(tmp_path: Path) -> None:
    """Re-running after a partial set leaves earlier migrations untouched."""

    (tmp_path / "0001_a.sql").write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, tmp_path)

    # Drop the table to simulate "if we re-applied 0001 it'd recreate".
    # Then add 0002 and re-run; only 0002 should fire (0001 is recorded).
    conn.execute("DROP TABLE a")
    (tmp_path / "0002_b.sql").write_text(
        "CREATE TABLE b (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    applied = apply_migrations(conn, tmp_path)
    assert applied == ["0002_b.sql"]
    assert not _table_exists(conn, "a")
    assert _table_exists(conn, "b")


def test_apply_raises_when_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    conn = sqlite3.connect(":memory:")
    with pytest.raises(FileNotFoundError):
        apply_migrations(conn, missing)
