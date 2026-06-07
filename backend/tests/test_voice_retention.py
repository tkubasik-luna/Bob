"""Tests for :class:`bob.voice_retention_policy.VoiceRetentionPolicy` (issue 0109).

Annexe E.3 — TWO SEPARATE caps on TWO tables:

- ``voice_audio_blobs`` bounded by total SIZE on disk (oldest first, file + row
  deleted);
- ``voice_turns`` bounded by AGE.

The two are independent: an audio sweep never touches the turn rows and a turn
sweep never touches the blobs. Both degrade gracefully (missing file, malformed
timestamp → treated as evictable, never a crash).
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.voice_retention_policy import (
    DEFAULT_RETENTION_POLICY,
    VoiceRetentionPolicy,
    enforce,
    get_retention_policy,
    set_retention_policy,
)
from bob.voice_store import VoiceStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[VoiceStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    yield VoiceStore(conn, tmp_path)
    conn.close()


@pytest.fixture(autouse=True)
def _restore_policy() -> Iterator[None]:
    saved = get_retention_policy()
    yield
    set_retention_policy(saved)


def _pcm(samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([1000] * samples))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --- defaults ----------------------------------------------------------------


def test_default_policy_enables_both_dimensions() -> None:
    assert DEFAULT_RETENTION_POLICY.max_audio_bytes is not None
    assert DEFAULT_RETENTION_POLICY.max_turn_age_seconds is not None


def test_set_get_round_trips() -> None:
    custom = VoiceRetentionPolicy(max_audio_bytes=42, max_turn_age_seconds=3.14)
    set_retention_policy(custom)
    assert get_retention_policy() == custom


# --- audio size cap ----------------------------------------------------------


def test_audio_evicted_oldest_first_file_and_row(store: VoiceStore) -> None:
    """Once total blob bytes exceed the cap, the oldest blobs (file + row) go."""

    b1 = store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)
    b2 = store.write_audio_blob(turn_id="t2", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)
    b3 = store.write_audio_blob(turn_id="t3", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)
    assert b1 is not None and b2 is not None and b3 is not None
    one = b1.bytes
    # Cap that fits ~2 of the 3 equal blobs → the oldest (b1) must be evicted.
    policy = VoiceRetentionPolicy(max_audio_bytes=one * 2 + 1, max_turn_age_seconds=None)

    outcome = enforce(store, policy)

    assert outcome.blobs_deleted == 1
    assert outcome.audio_bytes_freed == one
    # The oldest blob's row AND its file on disk are gone; the newer two remain.
    remaining = [b.id for b in store.list_blobs()]
    assert remaining == [b2.id, b3.id]
    assert not Path(b1.path).exists()
    assert Path(b2.path).exists()
    assert Path(b3.path).exists()


def test_audio_sweep_evicts_until_under_cap(store: VoiceStore) -> None:
    for i in range(5):
        store.write_audio_blob(turn_id=f"t{i}", kind="mic_in", pcm16=_pcm(100), sample_rate=16_000)
    total_before = store.total_audio_bytes()
    one = total_before // 5
    policy = VoiceRetentionPolicy(max_audio_bytes=one, max_turn_age_seconds=None)

    enforce(store, policy)

    assert store.total_audio_bytes() <= one
    # At least the oldest were removed; the newest survives.
    ids = [b.id for b in store.list_blobs()]
    assert ids == sorted(ids)


def test_audio_sweep_handles_missing_file(store: VoiceStore) -> None:
    """A blob whose file was wiped out-of-band still has its row dropped."""

    b1 = store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)
    assert b1 is not None
    Path(b1.path).unlink()  # simulate a manual disk wipe

    outcome = enforce(store, VoiceRetentionPolicy(max_audio_bytes=1, max_turn_age_seconds=None))

    assert outcome.blobs_deleted == 1
    assert store.list_blobs() == []


def test_audio_under_cap_is_noop(store: VoiceStore) -> None:
    store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(10), sample_rate=16_000)
    huge = VoiceRetentionPolicy(max_audio_bytes=10_000_000, max_turn_age_seconds=None)

    outcome = enforce(store, huge)

    assert outcome.blobs_deleted == 0
    assert len(store.list_blobs()) == 1


# --- turn age cap ------------------------------------------------------------


def test_turns_evicted_by_age(store: VoiceStore) -> None:
    now = datetime.now(UTC)
    store.write_turn(
        turn_id="old", started_at=_iso(now - timedelta(days=40)), end_reason="completed"
    )
    store.write_turn(
        turn_id="recent", started_at=_iso(now - timedelta(days=1)), end_reason="completed"
    )
    policy = VoiceRetentionPolicy(max_audio_bytes=None, max_turn_age_seconds=30 * 24 * 60 * 60)

    outcome = enforce(store, policy)

    assert outcome.turns_deleted == 1
    remaining = [t.turn_id for t in store.list_turns()]
    assert remaining == ["recent"]


def test_turn_with_malformed_timestamp_is_evicted(store: VoiceStore) -> None:
    store.write_turn(turn_id="bad", started_at="not-a-timestamp", end_reason="completed")
    policy = VoiceRetentionPolicy(max_audio_bytes=None, max_turn_age_seconds=1.0)

    outcome = enforce(store, policy)

    assert outcome.turns_deleted == 1
    assert store.list_turns() == []


def test_future_turn_is_kept(store: VoiceStore) -> None:
    future = datetime.now(UTC) + timedelta(days=10)
    store.write_turn(turn_id="future", started_at=future.isoformat(), end_reason="completed")
    policy = VoiceRetentionPolicy(max_audio_bytes=None, max_turn_age_seconds=1.0)

    outcome = enforce(store, policy)

    assert outcome.turns_deleted == 0
    assert [t.turn_id for t in store.list_turns()] == ["future"]


# --- separate caps (the load-bearing Annexe E.3 invariant) -------------------


def test_caps_are_independent(store: VoiceStore) -> None:
    """The audio (size) sweep and the turn (age) sweep never touch each other.

    Old turn rows with fresh small audio: an audio-only policy must keep ALL the
    (old) turn rows untouched while evicting audio by size, and vice versa.
    """

    now = datetime.now(UTC)
    # An OLD turn row with a tiny amount of audio under any sane size cap.
    store.write_turn(
        turn_id="old", started_at=_iso(now - timedelta(days=99)), end_reason="completed"
    )
    store.write_audio_blob(turn_id="old", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)

    # Audio-only policy (size tiny, age disabled): evicts the blob, KEEPS the row.
    audio_only = VoiceRetentionPolicy(max_audio_bytes=1, max_turn_age_seconds=None)
    out = enforce(store, audio_only)
    assert out.blobs_deleted == 1
    assert out.turns_deleted == 0
    assert [t.turn_id for t in store.list_turns()] == ["old"]
    assert store.list_blobs() == []

    # Re-seed audio; turn-only policy (age tiny, size disabled): evicts the row,
    # KEEPS the blob.
    store.write_audio_blob(turn_id="old", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)
    turn_only = VoiceRetentionPolicy(max_audio_bytes=None, max_turn_age_seconds=1.0)
    out2 = enforce(store, turn_only)
    assert out2.turns_deleted == 1
    assert out2.blobs_deleted == 0
    assert store.list_turns() == []
    assert len(store.list_blobs()) == 1


def test_none_caps_are_noop(store: VoiceStore) -> None:
    store.write_turn(turn_id="old", started_at="2000-01-01T00:00:00+00:00", end_reason="completed")
    store.write_audio_blob(turn_id="old", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)

    outcome = enforce(store, VoiceRetentionPolicy(max_audio_bytes=None, max_turn_age_seconds=None))

    assert outcome.blobs_deleted == 0
    assert outcome.turns_deleted == 0
    assert len(store.list_blobs()) == 1
    assert len(store.list_turns()) == 1


def test_enforce_uses_installed_singleton_when_no_policy_arg(store: VoiceStore) -> None:
    store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)
    set_retention_policy(VoiceRetentionPolicy(max_audio_bytes=1, max_turn_age_seconds=None))

    outcome = enforce(store)

    assert outcome.blobs_deleted == 1


def test_enforce_with_cleared_policy_is_noop(store: VoiceStore) -> None:
    store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(500), sample_rate=16_000)
    set_retention_policy(None)

    outcome = enforce(store)

    assert outcome.blobs_deleted == 0
    assert len(store.list_blobs()) == 1
