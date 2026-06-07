"""Tests for the 0010/0011 voice-persistence migrations (PRD 0016 / issue 0109).

``0010_voice_turns.sql`` and ``0011_voice_audio_blobs.sql`` create the two
Annexe E tables. The migrations must:

- create ``voice_turns`` with the exact Annexe E.1 columns,
- create ``voice_audio_blobs`` with the exact Annexe E.2 columns (an
  autoincrement ``id`` PK so the retention sweep evicts oldest-first),
- be idempotent at the runner level (re-running is a no-op).
"""

from __future__ import annotations

import sqlite3

from bob.db.migrations_runner import apply_migrations, default_migrations_dir

_TURNS = "0010_voice_turns.sql"
_BLOBS = "0011_voice_audio_blobs.sql"


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cursor = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cursor.fetchone() is not None


def test_migrations_create_both_tables() -> None:
    conn = sqlite3.connect(":memory:")
    applied = apply_migrations(conn, default_migrations_dir())

    assert _TURNS in applied
    assert _BLOBS in applied
    assert _table_exists(conn, "voice_turns")
    assert _table_exists(conn, "voice_audio_blobs")


def test_voice_turns_has_annexe_e1_columns() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())

    cols = set(_columns(conn, "voice_turns"))
    assert cols == {
        "turn_id",
        "jarvis_msg_id",
        "final_transcript",
        "spoken_text",
        "started_at",
        "ended_at",
        "end_reason",
        "draft_outcome",
        "latency_json",
    }


def test_voice_audio_blobs_has_annexe_e2_columns() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())

    cols = set(_columns(conn, "voice_audio_blobs"))
    assert cols == {"id", "turn_id", "kind", "path", "bytes", "created_at"}


def test_voice_audio_blobs_id_autoincrements() -> None:
    """The blob ``id`` is the eviction-order key — it must autoincrement."""

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())

    conn.execute(
        "INSERT INTO voice_audio_blobs(turn_id, kind, path, bytes, created_at) "
        "VALUES ('t1', 'mic_in', '/tmp/a.wav', 10, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO voice_audio_blobs(turn_id, kind, path, bytes, created_at) "
        "VALUES ('t1', 'tts_out', '/tmp/b.wav', 20, '2026-01-01T00:00:01+00:00')"
    )
    conn.commit()
    ids = [
        row[0] for row in conn.execute("SELECT id FROM voice_audio_blobs ORDER BY id").fetchall()
    ]
    assert ids == [1, 2]


def test_migrations_are_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    second_applied = apply_migrations(conn, default_migrations_dir())

    assert _TURNS not in second_applied
    assert _BLOBS not in second_applied
