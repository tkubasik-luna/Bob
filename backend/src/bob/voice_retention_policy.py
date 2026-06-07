"""Retention policy for persisted voice turns + audio blobs (issue 0109).

PRD 0016 / Annexe E.3. Voice persistence (:mod:`bob.voice_store`) accumulates
two kinds of data with very different cost profiles, so — unlike
:class:`bob.event_retention_policy.EventRetentionPolicy`, which caps ONE ring
buffer on two dimensions — this policy applies **two separate caps to two
separate tables**:

- ``voice_audio_blobs`` is bounded by **total SIZE on disk** (``max_audio_bytes``,
  default 1.5 GiB). Audio is the expensive part (megabytes per minute). When
  the summed ``bytes`` exceed the cap we evict the **oldest blobs first**
  (lowest ``id``), deleting BOTH the WAV file on disk AND its row, until the
  total is back under the cap.
- ``voice_turns`` is bounded by **AGE** (``max_turn_age_seconds``, default 30
  days). The text rows are tiny; we keep them for a fixed window for
  debug/replay/tuning and drop anything older.

Why size for audio but age for text? Audio is what fills the disk, so a hard
size ceiling is the right guard regardless of how old it is; the transcripts
are cheap and what a developer wants is "the last N days", a time window. The
two are deliberately decoupled (a turn row can outlive its audio, and vice
versa) — exactly the Annexe E.3 contract.

Both fields are nullable — ``None`` means "do not enforce this dimension" (a
no-op sweep), mirroring ``EventRetentionPolicy``. Eviction runs on demand:
:func:`enforce` is called right after each turn is persisted (and could be
called periodically). It is defensive — a missing file or a malformed
timestamp is treated as evictable, never a crash.

Singleton plumbing matches the event policy: the boot path (:mod:`bob.main`)
installs the default policy from settings; tests swap a tight one in to assert
eviction without writing gigabytes.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from bob.voice_store import VoiceAudioBlobRow, VoiceStore, VoiceTurnRow

_logger = structlog.get_logger(__name__)

#: 1.5 GiB — the default audio size ceiling (Annexe E.3). A few hours of 16 kHz
#: mic + 24 kHz TTS per day; this keeps roughly the last week or two on a
#: single-user desktop before the oldest recordings roll off.
DEFAULT_MAX_AUDIO_BYTES = int(1.5 * 1024 * 1024 * 1024)

#: 30 days — the default transcript age window (Annexe E.3).
DEFAULT_MAX_TURN_AGE_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class PurgeOutcome:
    """What one :func:`enforce` sweep evicted (for the persistence event)."""

    blobs_deleted: int
    turns_deleted: int
    audio_bytes_freed: int

    @property
    def anything(self) -> bool:
        """True when the sweep evicted at least one row (worth emitting)."""

        return self.blobs_deleted > 0 or self.turns_deleted > 0


@dataclass(frozen=True)
class VoiceRetentionPolicy:
    """Two separate caps on the voice persistence tables (Annexe E.3).

    ``max_audio_bytes`` bounds the summed on-disk size of ``voice_audio_blobs``;
    ``max_turn_age_seconds`` bounds the age of ``voice_turns`` rows. Both
    nullable so one dimension can be enforced without a placeholder for the
    other. The default policy installed by the boot path enables both with the
    PRD defaults.
    """

    max_audio_bytes: int | None = None
    max_turn_age_seconds: float | None = None


#: Default policy installed by :mod:`bob.main` (overridable via settings).
DEFAULT_RETENTION_POLICY = VoiceRetentionPolicy(
    max_audio_bytes=DEFAULT_MAX_AUDIO_BYTES,
    max_turn_age_seconds=DEFAULT_MAX_TURN_AGE_SECONDS,
)


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; ``None`` on garbage (caller treats as old)."""

    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    # The store writes tz-aware UTC; tolerate a naive value by assuming UTC.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _evict_audio_by_size(
    store: VoiceStore, max_audio_bytes: int, *, now_bytes: int | None = None
) -> tuple[int, int]:
    """Delete oldest blobs (file + row) until total size ≤ ``max_audio_bytes``.

    Returns ``(blobs_deleted, bytes_freed)``. Oldest-first is the lowest ``id``
    (the autoincrement key), which :meth:`VoiceStore.list_blobs` already orders
    by. A blob whose file is already gone still has its row dropped (and counts
    its recorded ``bytes`` as freed) so a manual disk wipe self-heals.
    """

    total = store.total_audio_bytes() if now_bytes is None else now_bytes
    if total <= max_audio_bytes:
        return 0, 0

    deleted = 0
    freed = 0
    # ``list_blobs()`` is oldest-first; walk forward deleting until under cap.
    for blob in store.list_blobs():
        if total <= max_audio_bytes:
            break
        _unlink_blob_file(blob)
        store.delete_blob(blob.id)
        total -= blob.bytes
        freed += blob.bytes
        deleted += 1
    return deleted, freed


def _unlink_blob_file(blob: VoiceAudioBlobRow) -> None:
    """Remove the WAV file for ``blob`` from disk (best-effort)."""

    with contextlib.suppress(OSError):
        Path(blob.path).unlink(missing_ok=True)


def _evict_turns_by_age(
    store: VoiceStore, max_turn_age_seconds: float, *, now: datetime | None = None
) -> int:
    """Delete ``voice_turns`` rows older than the age cap. Returns the count.

    Age is measured from ``started_at`` against ``now`` (UTC). A row with a
    malformed / unparseable ``started_at`` is treated as evictable (a corrupt
    timestamp should not pin a row forever) — mirrors the event policy's
    "malformed ts is evictable" rule. A future timestamp has negative age and
    is kept.
    """

    reference = now or datetime.now(UTC)
    deleted = 0
    for turn in store.list_turns():
        if _turn_is_expired(turn, reference, max_turn_age_seconds):
            store.delete_turn(turn.turn_id)
            deleted += 1
    return deleted


def _turn_is_expired(turn: VoiceTurnRow, reference: datetime, max_age_seconds: float) -> bool:
    started = _parse_iso(turn.started_at)
    if started is None:
        # Unparseable timestamp → evictable (never pin a corrupt row forever).
        return True
    age_seconds = (reference - started).total_seconds()
    return age_seconds > max_age_seconds


def enforce(store: VoiceStore, policy: VoiceRetentionPolicy | None = None) -> PurgeOutcome:
    """Apply ``policy`` (or the installed singleton) to ``store``. On-demand.

    Audio is capped by size (oldest-first, file + row), turns by age — the two
    sweeps are independent (Annexe E.3 separate caps). A ``None`` cap on either
    dimension skips that sweep. Never raises: a filesystem hiccup or a malformed
    row degrades to "treat as evictable / skip" so the persist path that calls
    this can't be taken down by retention.
    """

    effective = policy if policy is not None else get_retention_policy()
    if effective is None:
        return PurgeOutcome(blobs_deleted=0, turns_deleted=0, audio_bytes_freed=0)

    blobs_deleted = 0
    bytes_freed = 0
    turns_deleted = 0
    if effective.max_audio_bytes is not None:
        blobs_deleted, bytes_freed = _evict_audio_by_size(store, effective.max_audio_bytes)
    if effective.max_turn_age_seconds is not None:
        turns_deleted = _evict_turns_by_age(store, effective.max_turn_age_seconds)

    outcome = PurgeOutcome(
        blobs_deleted=blobs_deleted,
        turns_deleted=turns_deleted,
        audio_bytes_freed=bytes_freed,
    )
    if outcome.anything:
        _logger.info(
            "voice_retention.purged",
            blobs_deleted=blobs_deleted,
            turns_deleted=turns_deleted,
            audio_bytes_freed=bytes_freed,
        )
    return outcome


_DEFAULT_POLICY: VoiceRetentionPolicy | None = DEFAULT_RETENTION_POLICY


def set_retention_policy(policy: VoiceRetentionPolicy | None) -> None:
    """Install (or clear) the process-wide voice retention policy singleton.

    ``None`` disables retention enforcement (an :func:`enforce` becomes a no-op).
    """

    global _DEFAULT_POLICY
    _DEFAULT_POLICY = policy


def get_retention_policy() -> VoiceRetentionPolicy | None:
    """Return the currently installed policy, or ``None`` if disabled."""

    return _DEFAULT_POLICY


__all__ = [
    "DEFAULT_MAX_AUDIO_BYTES",
    "DEFAULT_MAX_TURN_AGE_SECONDS",
    "DEFAULT_RETENTION_POLICY",
    "PurgeOutcome",
    "VoiceRetentionPolicy",
    "enforce",
    "get_retention_policy",
    "set_retention_policy",
]
