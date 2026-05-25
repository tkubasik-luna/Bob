"""SQLite-backed store for persisted :class:`RollingSummary` rows.

PRD 0006 / issue 0046. The bounded context policy injects a single rolling
summary block ahead of the recent-turns window. Generating that summary
costs an LLM call so we persist the result and only regenerate when the
window slides past a configurable trigger.

Persistence schema lives in migration ``0006_rolling_summaries.sql``. The
table is append-only — every regeneration inserts a new row stamped with
the ``summariser_version`` and ``(from_turn, to_turn)`` range. The provider
asks the store for the latest row at assembly time.

Issue 0051 adds the ``epoch_id`` column on the same table (migration
``0007_epoch_id_columns.sql``). The store exposes the new field on
:class:`StoredRollingSummary` and a :meth:`latest_for_epoch` filter so
the epoch manager can reason about the current-epoch summary in
isolation.

The store is intentionally tiny: ``append``, ``latest``, ``clear`` and a
``count`` helper for tests. Mirrors the shape of :class:`JarvisStore`.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredRollingSummary:
    """One row of the ``rolling_summaries`` table.

    Fields mirror the SQL schema. ``id`` is the autoincrement primary key;
    callers should treat the value as opaque.

    ``epoch_id`` (PRD 0006 / issue 0051) groups summaries by epoch. Rows
    written before migration ``0007_epoch_id_columns.sql`` were backfilled
    to ``epoch_id = 0``; new rows inherit the orchestrator's current
    epoch id at append time.
    """

    id: int
    from_turn: int
    to_turn: int
    summariser_version: int
    text: str
    token_estimate: int
    created_at: str
    epoch_id: int = 0


class RollingSummaryStore:
    """Append-only SQLite-backed store of :class:`StoredRollingSummary` rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def append(
        self,
        *,
        from_turn: int,
        to_turn: int,
        summariser_version: int,
        text: str,
        token_estimate: int = 0,
        epoch_id: int = 0,
    ) -> int:
        """Insert a new summary row and return its primary key.

        ``epoch_id`` (PRD 0006 / issue 0051) defaults to ``0`` so legacy
        call sites (issue 0046 pipeline) keep their semantics — every
        pre-seal write lands in epoch 0. The orchestrator's epoch
        manager passes the live epoch id when issuing a fresh write
        after a seal.
        """

        if from_turn < 1 or to_turn < from_turn:
            raise ValueError(f"Invalid summary range from_turn={from_turn}, to_turn={to_turn}")
        if epoch_id < 0:
            raise ValueError(f"epoch_id must be >= 0, got {epoch_id}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO rolling_summaries(from_turn, to_turn, summariser_version, "
                "text, token_estimate, epoch_id) VALUES (?, ?, ?, ?, ?, ?)",
                (from_turn, to_turn, summariser_version, text, token_estimate, epoch_id),
            )
        row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover — sqlite3 always returns an id
            raise RuntimeError("rolling_summaries INSERT returned no lastrowid")
        return row_id

    def latest(self) -> StoredRollingSummary | None:
        """Return the freshest row (highest ``id``), or ``None`` if empty."""

        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, from_turn, to_turn, summariser_version, text, "
                "token_estimate, created_at, epoch_id FROM rolling_summaries "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return StoredRollingSummary(
            id=row[0],
            from_turn=row[1],
            to_turn=row[2],
            summariser_version=row[3],
            text=row[4],
            token_estimate=row[5],
            created_at=row[6],
            epoch_id=row[7],
        )

    def latest_for_epoch(self, epoch_id: int) -> StoredRollingSummary | None:
        """Return the freshest row tagged with ``epoch_id`` (or ``None``).

        Added for issue 0051: the :class:`EpochManager` only considers
        the current epoch's rolling summary for the seal decision, and
        the cross-epoch digest rebuild filters by epoch. Pre-0051 call
        sites use :meth:`latest`.
        """

        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, from_turn, to_turn, summariser_version, text, "
                "token_estimate, created_at, epoch_id FROM rolling_summaries "
                "WHERE epoch_id = ? ORDER BY id DESC LIMIT 1",
                (epoch_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return StoredRollingSummary(
            id=row[0],
            from_turn=row[1],
            to_turn=row[2],
            summariser_version=row[3],
            text=row[4],
            token_estimate=row[5],
            created_at=row[6],
            epoch_id=row[7],
        )

    def count(self) -> int:
        """Return the number of rows persisted. Test helper."""

        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM rolling_summaries")
            return int(cursor.fetchone()[0])

    def clear(self) -> None:
        """Drop every persisted summary. Resets the AUTOINCREMENT counter."""

        with self._lock, self._conn:
            self._conn.execute("DELETE FROM rolling_summaries")
            self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'rolling_summaries'")
