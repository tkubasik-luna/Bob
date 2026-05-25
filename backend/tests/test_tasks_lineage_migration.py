"""Tests for the 0005 ``tasks.lineage`` migration.

PRD 0006 / issue 0044 reserves the ``lineage`` column for a future
``replan_task`` tool. The migration must:

- add the column with a default of ``'[]'`` (JSON-text empty list),
- back-fill existing rows so a row written before the migration still
  reads a valid empty list,
- be idempotent at the runner level (re-running is a no-op).
"""

from __future__ import annotations

import json
import sqlite3

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.task_store import TaskStore


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_migration_adds_lineage_column() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())

    cols = _columns(conn, "tasks")
    assert "lineage" in cols


def test_migration_defaults_existing_rows_to_empty_list() -> None:
    """A task row inserted via the pre-0005 schema must read ``lineage=[]``."""

    conn = sqlite3.connect(":memory:")

    # Apply migrations 0001 → 0004 manually, record them in _migrations,
    # seed a task, then apply migrations (which will run 0005). The new
    # column lands with the default ``'[]'`` so the task surfaces with
    # ``lineage=[]``.
    pre_0005_sql = """
    CREATE TABLE IF NOT EXISTS jarvis_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
        content TEXT NOT NULL,
        action TEXT CHECK (action IN ('done','ask_user','progress')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        goal TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','running','waiting_input','done','failed')),
        needs_attention INTEGER NOT NULL DEFAULT 0,
        result TEXT,
        parent_task_id TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        dismissed INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
    );
    """
    conn.executescript(pre_0005_sql)
    conn.execute(
        "INSERT INTO tasks(id, title, goal, state) VALUES (?, ?, ?, ?)",
        ("legacy-1", "Legacy task", "Old goal", "pending"),
    )
    conn.commit()

    # Pre-record every migration except 0005 (and 0004 which only touches
    # ``jarvis_messages``) so apply_migrations focuses on 0005 + newer.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations("
        "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    targeted = "0005_tasks_lineage.sql"
    for path in sorted(default_migrations_dir().glob("*.sql")):
        if path.name == targeted:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO _migrations(filename, applied_at) VALUES (?, datetime('now'))",
            (path.name,),
        )
    conn.commit()

    applied = apply_migrations(conn, default_migrations_dir())
    assert targeted in applied

    raw = conn.execute("SELECT lineage FROM tasks WHERE id = ?", ("legacy-1",)).fetchone()
    assert raw == ("[]",)
    assert json.loads(raw[0]) == []


def test_task_store_round_trips_explicit_lineage() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    store = TaskStore(conn)

    task_id = store.create_task(
        title="Replanned",
        goal="New goal",
        lineage=["old-1", "old-2"],
    )

    task = store.get_task(task_id)
    assert task.lineage == ["old-1", "old-2"]


def test_task_store_defaults_lineage_to_empty_list() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    store = TaskStore(conn)

    task_id = store.create_task(title="Fresh", goal="Do thing")
    assert store.get_task(task_id).lineage == []


def test_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    second_applied = apply_migrations(conn, default_migrations_dir())
    assert "0005_tasks_lineage.sql" not in second_applied
