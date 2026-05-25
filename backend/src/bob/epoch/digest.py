"""Cross-epoch digest — rebuild from RAW sealed turns, store the freshest row.

PRD 0006 / issue 0051. Whenever an epoch seals the orchestrator
regenerates the cross-epoch digest from RAW sealed turns (NOT from
prior digests — bounded drift, see PRD's "Further Notes"). The digest
is then injected into the active prompt by
:class:`bob.context.providers.cross_epoch_digest.CrossEpochDigestProvider`.

This module exposes:

* :class:`CrossEpochDigest` — frozen dataclass describing one persisted
  digest row.
* :class:`CrossEpochDigestStore` — append-only SQLite store mirroring
  :class:`bob.rolling_summary_store.RollingSummaryStore`.
* :func:`regenerate_cross_epoch_digest` — pure rebuild function. Takes
  the sealed turns + policy + tokenizer; returns the new digest text.
  Never reads the prior digest row.

Why a separate table rather than reusing ``rolling_summaries``: the
rolling-summary store already carries per-epoch entries; the cross-
epoch digest is conceptually a different beast (it folds in multiple
sealed epochs at once, gets rebuilt from RAW turns on every seal, and
is what the active prompt actually injects). Keeping the two stores
distinct makes the read path crisp: "the freshest digest is whatever
``CrossEpochDigestStore.latest()`` returns; sealed epochs sit untouched
in ``rolling_summaries``."
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from bob.context.entry import ContextEntry
from bob.epoch.policy import EpochPolicy

#: Header line prepended to the digest body so the LLM sees a clear
#: "this is a cross-epoch summary, not the current conversation"
#: signal. Kept short on purpose — the bounded policy's prompt budget
#: is the constraint.
CROSS_EPOCH_DIGEST_HEADER = "Synthèse des époques passées (reconstituée à partir des tours bruts) :"


@dataclass(frozen=True)
class CrossEpochDigest:
    """One persisted row of the ``cross_epoch_digests`` table.

    Fields:

    - ``id`` — autoincrement primary key. Opaque.
    - ``text`` — rendered digest body (already includes the header).
    - ``summariser_version`` — stamp from :class:`EpochPolicy`.
    - ``sealed_epoch_count`` — number of sealed epochs folded into this
      digest. Used by the long-session test to assert digest input
      grows with seals (i.e. the regenerator did see ALL sealed turns,
      not just the latest epoch).
    - ``token_estimate`` — rough size signal for the assembler.
    - ``created_at`` — ISO timestamp, autopopulated by sqlite.
    """

    id: int
    text: str
    summariser_version: int
    sealed_epoch_count: int
    token_estimate: int
    created_at: str


class CrossEpochDigestStore:
    """Append-only SQLite-backed store for :class:`CrossEpochDigest` rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def append(
        self,
        *,
        text: str,
        summariser_version: int,
        sealed_epoch_count: int,
        token_estimate: int = 0,
    ) -> int:
        """Insert a new digest row and return its primary key."""

        if sealed_epoch_count < 0:
            raise ValueError(f"sealed_epoch_count must be >= 0, got {sealed_epoch_count}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO cross_epoch_digests(text, summariser_version, "
                "sealed_epoch_count, token_estimate) VALUES (?, ?, ?, ?)",
                (text, summariser_version, sealed_epoch_count, token_estimate),
            )
        row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover — sqlite3 always returns an id
            raise RuntimeError("cross_epoch_digests INSERT returned no lastrowid")
        return row_id

    def latest(self) -> CrossEpochDigest | None:
        """Return the freshest digest row (highest ``id``), or ``None``."""

        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, text, summariser_version, sealed_epoch_count, "
                "token_estimate, created_at FROM cross_epoch_digests "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return CrossEpochDigest(
            id=row[0],
            text=row[1],
            summariser_version=row[2],
            sealed_epoch_count=row[3],
            token_estimate=row[4],
            created_at=row[5],
        )

    def count(self) -> int:
        """Test helper — return the number of persisted digest rows."""

        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM cross_epoch_digests")
            return int(cursor.fetchone()[0])

    def clear(self) -> None:
        """Drop every digest row. Test helper."""

        with self._lock, self._conn:
            self._conn.execute("DELETE FROM cross_epoch_digests")
            self._conn.execute("DELETE FROM sqlite_sequence WHERE name = 'cross_epoch_digests'")


def regenerate_cross_epoch_digest(
    *,
    sealed_turns: Sequence[ContextEntry],
    sealed_epoch_count: int,
    policy: EpochPolicy,
) -> str:
    """Rebuild the cross-epoch digest from RAW sealed turns.

    PURE FUNCTION — no I/O, no LLM call. The digest is composed
    deterministically from the rendered transcript of ``sealed_turns``,
    then truncated to ``policy.max_digest_size`` characters. Tests assert
    that consecutive seals always pass RAW :class:`ContextEntry` rows in
    (never the prior digest), which is the central drift-bounding
    invariant of issue 0051.

    Why no LLM call: the rolling-summary path inside each epoch already
    uses :class:`bob.context.summariser.LLMSummariser` against RAW turns.
    The cross-epoch digest sits on top of that and would compound LLM
    drift if we re-summarised at this layer. Composing from raw turns
    + a fixed cap keeps the digest deterministic; the v2 real-RAG path
    can replace this with an embedding-based summary without changing
    the contract.

    Args:

    - ``sealed_turns`` — every :class:`ContextEntry` from sealed epochs,
      in chronological order. The caller (typically
      :meth:`EpochManager.apply_seal`) gathers these from
      :class:`bob.jarvis_store.JarvisStore` filtered on sealed
      ``epoch_id``s.
    - ``sealed_epoch_count`` — informational. Stamped on the persisted
      row for the long-session test assertion.
    - ``policy`` — drives ``max_digest_size`` cap.

    Returns the rendered digest text (header + transcript, truncated).
    Empty when ``sealed_turns`` is empty.
    """

    if not sealed_turns:
        return ""

    lines: list[str] = [CROSS_EPOCH_DIGEST_HEADER]
    for entry in sealed_turns:
        role = entry.payload.get("role")
        content = entry.payload.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            continue
        lines.append(f"- {role.upper()}: {content}")
    body = "\n".join(lines)

    if policy.max_digest_size > 0 and len(body) > policy.max_digest_size:
        # Truncate keeping the header intact. The trailing ellipsis is
        # important — the LLM should see we deliberately cut, not that
        # the digest is mysteriously short.
        truncated = body[: policy.max_digest_size - 1].rstrip()
        body = truncated + "…"
    return body
