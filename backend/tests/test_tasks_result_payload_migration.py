"""Tests for the 0009 ``tasks.result_payload`` migration (PRD 0008 / issue 0064).

The migration adds a nullable JSON-text column holding the structured
deliverable descriptor (``{"component": ..., "props": {...}}``) the sub-agent
emitted in ``done.ui_payload``. The migration must:

- add the column to ``tasks``,
- leave pre-0009 rows valid with no back-fill (``NULL`` → ``result_payload``
  decodes to ``None`` so callers fall back to the ``result`` text),
- be idempotent at the runner level (re-running is a no-op).
"""

from __future__ import annotations

import sqlite3

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.task_store import TaskStore

_TARGET = "0009_tasks_result_payload.sql"


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_migration_adds_result_payload_column() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())

    assert "result_payload" in _columns(conn, "tasks")


def test_pre_0009_row_reads_result_payload_none() -> None:
    """A task row written before 0009 must surface ``result_payload=None``
    (the column is nullable and additive — no back-fill UPDATE needed)."""

    conn = sqlite3.connect(":memory:")

    # Apply every migration EXCEPT 0009, seed a row through the resulting
    # schema, then run 0009 and confirm the legacy row decodes cleanly.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations("
        "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for path in sorted(default_migrations_dir().glob("*.sql")):
        if path.name == _TARGET:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT OR IGNORE INTO _migrations(filename, applied_at) VALUES (?, datetime('now'))",
            (path.name,),
        )
    conn.commit()

    conn.execute(
        "INSERT INTO tasks(id, title, goal, state) VALUES (?, ?, ?, ?)",
        ("legacy-1", "Legacy task", "Old goal", "done"),
    )
    conn.execute(
        "UPDATE tasks SET result = ? WHERE id = ?",
        ("legacy markdown result", "legacy-1"),
    )
    conn.commit()

    applied = apply_migrations(conn, default_migrations_dir())
    assert _TARGET in applied

    store = TaskStore(conn)
    task = store.get_task("legacy-1")
    assert task.result == "legacy markdown result"
    assert task.result_payload is None


def test_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    second_applied = apply_migrations(conn, default_migrations_dir())
    assert _TARGET not in second_applied
