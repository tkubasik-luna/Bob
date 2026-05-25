"""Tests for the 0004 ContextEntry migration on ``jarvis_messages``.

The migration extends the existing ``jarvis_messages`` table in-place with
the ``ContextEntry`` field set (``kind``, ``source``, ``token_estimate``,
``pinned``, ``provider_id``, ``payload``, ``schema_version``) and backfills
existing rows. It must be idempotent.
"""

from __future__ import annotations

import json
import sqlite3

from bob.db.migrations_runner import apply_migrations, default_migrations_dir


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _apply_to_fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return conn


def test_migration_adds_context_entry_columns() -> None:
    conn = _apply_to_fresh_db()
    cols = _columns(conn, "jarvis_messages")

    for expected in (
        "kind",
        "source",
        "token_estimate",
        "pinned",
        "provider_id",
        "payload",
        "schema_version",
    ):
        assert expected in cols, f"missing column {expected} in jarvis_messages: {cols}"


def test_migration_backfills_existing_rows() -> None:
    """Apply 0001-0003 manually, seed rows, then apply 0004 and assert backfill."""

    conn = sqlite3.connect(":memory:")

    # Apply only the pre-0004 migrations by replaying their SQL directly,
    # so we can write rows in the pre-0043 schema before the new columns
    # exist, then apply the full migration set (including 0004).
    pre_0004_sql = """
    CREATE TABLE IF NOT EXISTS jarvis_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
        content TEXT NOT NULL,
        action TEXT CHECK (action IN ('done','ask_user','progress')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """
    conn.executescript(pre_0004_sql)
    conn.execute(
        "INSERT INTO jarvis_messages(role, content) VALUES (?, ?)",
        ("user", "Salut Bob"),
    )
    conn.execute(
        "INSERT INTO jarvis_messages(role, content) VALUES (?, ?)",
        ("assistant", "Salut Tom"),
    )
    conn.execute(
        "INSERT INTO jarvis_messages(role, content) VALUES (?, ?)",
        ("tool", "weather=ok"),
    )
    conn.commit()

    # Pretend the runner has applied 0001-0003 — record them in the
    # bookkeeping table so apply_migrations skips them and runs only 0004.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations("
        "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for prior_filename in (
        "0001_jarvis_messages.sql",
        "0002_tasks.sql",
        "0003_tasks_dismissed.sql",
    ):
        conn.execute(
            "INSERT OR IGNORE INTO _migrations(filename, applied_at) VALUES (?, datetime('now'))",
            (prior_filename,),
        )
    conn.commit()

    applied = apply_migrations(conn, default_migrations_dir())
    assert "0004_jarvis_messages_context_entry.sql" in applied

    rows = conn.execute(
        "SELECT role, content, kind, source, provider_id, schema_version, "
        "token_estimate, pinned, payload FROM jarvis_messages ORDER BY id"
    ).fetchall()

    assert len(rows) == 3

    role0, content0, kind0, source0, pid0, sv0, te0, pinned0, payload0 = rows[0]
    assert role0 == "user"
    assert content0 == "Salut Bob"
    assert kind0 == "user_turn"
    assert source0 == "jarvis_store"
    assert pid0 == "legacy_full_history"
    assert sv0 == 1
    assert te0 == len(content0) // 4
    assert pinned0 == 0
    parsed = json.loads(payload0)
    assert parsed == {"role": "user", "content": "Salut Bob"}

    assert rows[1][2] == "assistant_turn"
    assert rows[2][2] == "system_note"  # tool row collapses to system_note


def test_migration_is_idempotent_at_runner_level() -> None:
    conn = _apply_to_fresh_db()
    second = apply_migrations(conn, default_migrations_dir())
    assert second == []


def test_migration_records_filename_in_bookkeeping_table() -> None:
    conn = _apply_to_fresh_db()
    applied = conn.execute("SELECT filename FROM _migrations ORDER BY filename").fetchall()
    filenames = [row[0] for row in applied]
    assert "0004_jarvis_messages_context_entry.sql" in filenames
