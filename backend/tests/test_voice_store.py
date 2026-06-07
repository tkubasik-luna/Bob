"""Tests for :class:`bob.voice_store.VoiceStore` (PRD 0016 / issue 0109).

Round-trips a finalized voice turn through the store:

- ``write_turn`` + ``get_turn`` preserve the Annexe E.1 fields,
- ``write_audio_blob`` writes a real WAV file on disk and records its path +
  byte size (Annexe E.2),
- ``link_jarvis_msg`` stamps the Jarvis-history link,
- the reads the retention sweep relies on (``list_blobs`` oldest-first,
  ``total_audio_bytes``, ``list_turns``) return what was written.
"""

from __future__ import annotations

import sqlite3
import struct
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.voice_store import VoiceStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[VoiceStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    yield VoiceStore(conn, tmp_path)
    conn.close()


def _pcm(samples: int, *, amplitude: int = 1000) -> bytes:
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


def test_write_turn_round_trips(store: VoiceStore) -> None:
    store.write_turn(
        turn_id="t1",
        started_at="2026-06-07T10:00:00+00:00",
        ended_at="2026-06-07T10:00:05+00:00",
        final_transcript="quel temps fait il",
        spoken_text="il fait beau",
        end_reason="completed",
        draft_outcome="none",
        latency_json='{"marks": {}, "derived": {}}',
    )

    row = store.get_turn("t1")
    assert row is not None
    assert row.turn_id == "t1"
    assert row.final_transcript == "quel temps fait il"
    assert row.spoken_text == "il fait beau"
    assert row.end_reason == "completed"
    assert row.draft_outcome == "none"
    assert row.latency_json == '{"marks": {}, "derived": {}}'
    assert row.jarvis_msg_id is None


def test_write_turn_is_idempotent_on_turn_id(store: VoiceStore) -> None:
    """A racing second finalize (INSERT OR REPLACE) replaces, never duplicates."""

    store.write_turn(turn_id="t1", started_at="2026-06-07T10:00:00+00:00", end_reason="voice_stop")
    store.write_turn(turn_id="t1", started_at="2026-06-07T10:00:00+00:00", end_reason="completed")

    turns = store.list_turns()
    assert len(turns) == 1
    assert turns[0].end_reason == "completed"


def test_link_jarvis_msg(store: VoiceStore) -> None:
    store.write_turn(turn_id="t1", started_at="2026-06-07T10:00:00+00:00", end_reason="completed")
    store.link_jarvis_msg("t1", "42")

    row = store.get_turn("t1")
    assert row is not None
    assert row.jarvis_msg_id == "42"


def test_write_audio_blob_writes_wav_file_and_row(store: VoiceStore, tmp_path: Path) -> None:
    pcm = _pcm(480)  # 480 s16le samples
    blob = store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=pcm, sample_rate=16_000)

    assert blob is not None
    assert blob.kind == "mic_in"
    assert blob.turn_id == "t1"
    # The path points at a real WAV file on disk under the data dir.
    path = Path(blob.path)
    assert path.exists()
    assert path.parent == tmp_path / "voice_audio"
    assert blob.bytes == path.stat().st_size
    # The file is a valid mono s16le WAV at the requested rate, content intact.
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.readframes(wav.getnframes()) == pcm

    # The row is queryable and carries the path.
    blobs = store.list_blobs("t1")
    assert [b.id for b in blobs] == [blob.id]
    assert blobs[0].path == str(path)


def test_write_audio_blob_skips_empty_pcm(store: VoiceStore) -> None:
    """An empty recording writes no file and no row (never a zero-byte WAV)."""

    blob = store.write_audio_blob(turn_id="t1", kind="tts_out", pcm16=b"", sample_rate=24_000)
    assert blob is None
    assert store.list_blobs("t1") == []
    assert store.total_audio_bytes() == 0


def test_total_audio_bytes_sums_all_blobs(store: VoiceStore) -> None:
    b1 = store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(100), sample_rate=16_000)
    b2 = store.write_audio_blob(turn_id="t1", kind="tts_out", pcm16=_pcm(200), sample_rate=24_000)
    assert b1 is not None and b2 is not None
    assert store.total_audio_bytes() == b1.bytes + b2.bytes


def test_list_blobs_is_oldest_first(store: VoiceStore) -> None:
    store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(10), sample_rate=16_000)
    store.write_audio_blob(turn_id="t2", kind="mic_in", pcm16=_pcm(10), sample_rate=16_000)
    store.write_audio_blob(turn_id="t3", kind="mic_in", pcm16=_pcm(10), sample_rate=16_000)

    ids = [b.id for b in store.list_blobs()]
    assert ids == sorted(ids)  # autoincrement → ascending id == oldest-first


def test_delete_blob_and_turn(store: VoiceStore) -> None:
    blob = store.write_audio_blob(turn_id="t1", kind="mic_in", pcm16=_pcm(10), sample_rate=16_000)
    store.write_turn(turn_id="t1", started_at="2026-06-07T10:00:00+00:00", end_reason="completed")
    assert blob is not None

    store.delete_blob(blob.id)
    store.delete_turn("t1")
    assert store.list_blobs() == []
    assert store.get_turn("t1") is None
