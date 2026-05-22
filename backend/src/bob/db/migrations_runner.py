"""Idempotent SQLite migration runner.

Discovers ``*.sql`` files under a directory (sorted by filename, lexically),
applies any that haven't already been recorded, and tracks applied filenames
in a ``_migrations`` table so re-running is a no-op.

Design constraints (kept small on purpose):

* Single transaction per migration file. If a migration fails mid-way, nothing
  from that file is committed and the bookkeeping row is not inserted, so a
  subsequent run retries it.
* Migrations are append-only: the runner never edits or deletes ``*.sql``
  files. Schema changes ship as new files with a higher prefix
  (``0002_…``, ``0003_…``, …).
* The runner is intentionally synchronous — boot-time concern, runs once.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import structlog

_logger = structlog.get_logger(__name__)


_BOOKKEEPING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _migrations (
    filename TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
""".strip()


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(_BOOKKEEPING_TABLE_SQL)
    conn.commit()


def _applied_filenames(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT filename FROM _migrations")
    return {row[0] for row in cursor.fetchall()}


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """Apply every pending ``*.sql`` file under ``migrations_dir`` in order.

    Returns the list of filenames newly applied during this call. Re-running
    on a fully-migrated database is a no-op and returns ``[]``.

    Raises :class:`FileNotFoundError` if ``migrations_dir`` does not exist —
    the boot path passes a path under the project tree so we want loud
    failure rather than a silent skip if the folder is missing.
    """

    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations directory not found: {migrations_dir}")

    _ensure_bookkeeping(conn)
    already_applied = _applied_filenames(conn)

    files = sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())
    newly_applied: list[str] = []

    for path in files:
        if path.name in already_applied:
            continue

        sql = path.read_text(encoding="utf-8")
        try:
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO _migrations(filename, applied_at) VALUES (?, ?)",
                    (path.name, datetime.now(UTC).isoformat()),
                )
        except sqlite3.DatabaseError:
            _logger.exception("migrations.apply_failed", filename=path.name)
            raise

        newly_applied.append(path.name)
        _logger.info("migrations.applied", filename=path.name)

    return newly_applied


def default_migrations_dir() -> Path:
    """Return the bundled migrations directory next to this module."""

    return Path(__file__).resolve().parent / "migrations"
