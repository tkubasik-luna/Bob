"""SQLite + on-disk store for finalized full-duplex voice turns (issue 0109).

PRD 0016 / Annexe E. A real-time voice turn finalizes in the full-duplex loop
(:mod:`bob.voice_loop`) and, at that moment, the WS layer persists it here so it
can be replayed / latency-tuned offline without keeping audio in RAM:

- the ``voice_turns`` row (migration 0010) — transcript, what Bob actually
  spoke, the end reason, the latency marks, the Jarvis-history link;
- zero or more ``voice_audio_blobs`` (migration 0011) — the mic input
  (``mic_in``) and Bob's reply (``tts_out``) as **WAV files on disk** under
  ``{BOB_DATA_DIR}/voice_audio/``; only the path + byte size live in the DB.

Why files on disk and not blobs in SQLite? A few minutes of 16 kHz mono PCM is
megabytes; inlining it would bloat the single-file Jarvis DB and make the
size-bounded retention sweep (:class:`bob.voice_retention_policy.VoiceRetentionPolicy`)
walk huge rows. Keeping the audio as ordinary files lets retention delete a
file + its row cheaply, and lets a developer play a WAV straight from disk.

Thread-safety mirrors :class:`bob.jarvis_store.JarvisStore`: the connection is
opened with ``check_same_thread=False`` by the boot path and a per-store lock
serialises writes so FastAPI request workers cannot interleave statements.

Singleton plumbing matches the other stores: the boot path
(:mod:`bob.main`) opens the connection, runs migrations and primes the store
via :func:`set_default_store`; tests and the attest harness do the same against
a tmp DB / dir.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

_logger = structlog.get_logger(__name__)

#: The two audio recordings a turn can carry (Annexe E.2 ``kind``).
BlobKind = Literal["mic_in", "tts_out"]

#: End reasons a finalized turn can carry (Annexe E.1 ``end_reason``).
EndReason = Literal["completed", "bargein", "voice_stop", "error"]

#: Sub-directory under ``BOB_DATA_DIR`` that holds the WAV files.
AUDIO_SUBDIR = "voice_audio"

#: PCM contract for the WAV files we write: 16-bit signed, mono. The sample
#: rate is per-blob (mic is 16 kHz, Kokoro TTS is 24 kHz) so it is passed in.
_WAV_SAMPLE_WIDTH_BYTES = 2
_WAV_CHANNELS = 1


def _now_iso() -> str:
    """ISO-8601 UTC timestamp — the wall-clock format the other tables use."""

    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class VoiceTurnRow:
    """One persisted ``voice_turns`` row (Annexe E.1)."""

    turn_id: str
    jarvis_msg_id: str | None
    final_transcript: str | None
    spoken_text: str | None
    started_at: str
    ended_at: str | None
    end_reason: str | None
    draft_outcome: str | None
    latency_json: str | None


@dataclass(frozen=True)
class VoiceAudioBlobRow:
    """One persisted ``voice_audio_blobs`` row (Annexe E.2)."""

    id: int
    turn_id: str
    kind: str
    path: str
    bytes: int
    created_at: str


class VoiceStore:
    """Persistent store for finalized voice turns + their audio blobs.

    Construct with a :class:`sqlite3.Connection` (migrations already applied)
    and the resolved ``BOB_DATA_DIR``; the WAV files are written under
    ``{data_dir}/voice_audio/``. All public methods are safe to call from any
    FastAPI worker thread (a module-level lock serialises writes).
    """

    def __init__(self, conn: sqlite3.Connection, data_dir: Path) -> None:
        self._conn = conn
        self._data_dir = data_dir
        self._audio_dir = data_dir / AUDIO_SUBDIR
        self._lock = threading.Lock()

    # -- writes --------------------------------------------------------------

    def write_turn(
        self,
        *,
        turn_id: str,
        started_at: str,
        final_transcript: str | None = None,
        spoken_text: str | None = None,
        ended_at: str | None = None,
        end_reason: str | None = None,
        draft_outcome: str | None = "none",
        latency_json: str | None = None,
        jarvis_msg_id: str | None = None,
    ) -> None:
        """Insert (or replace) the ``voice_turns`` row for ``turn_id``.

        ``INSERT OR REPLACE`` keeps the write idempotent on the ``turn_id`` PK
        so a racing finalize (``voice_stop`` + socket-close) cannot double-row.
        ``draft_outcome`` defaults to ``"none"`` (the speculative Draft is issue
        0104; until then no turn has a draft outcome).
        """

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO voice_turns(
                    turn_id, jarvis_msg_id, final_transcript, spoken_text,
                    started_at, ended_at, end_reason, draft_outcome, latency_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    jarvis_msg_id,
                    final_transcript,
                    spoken_text,
                    started_at,
                    ended_at,
                    end_reason,
                    draft_outcome,
                    latency_json,
                ),
            )

    def link_jarvis_msg(self, turn_id: str, jarvis_msg_id: str) -> None:
        """Set ``voice_turns.jarvis_msg_id`` for an existing turn (Annexe E.1).

        The final transcript enters the Jarvis thread as a turn; this records
        which thread entry it became so the persisted voice turn and the
        conversational history cross-reference. A no-op if the turn row is
        absent (defensive — narrow test setups).
        """

        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE voice_turns SET jarvis_msg_id = ? WHERE turn_id = ?",
                (jarvis_msg_id, turn_id),
            )

    def write_audio_blob(
        self,
        *,
        turn_id: str,
        kind: BlobKind,
        pcm16: bytes,
        sample_rate: int,
    ) -> VoiceAudioBlobRow | None:
        """Write ``pcm16`` as a WAV file on disk and record its row (Annexe E.2).

        Returns the inserted :class:`VoiceAudioBlobRow` (with the resolved path +
        byte size), or ``None`` when ``pcm16`` is empty (a turn with no captured
        audio of this kind simply has no blob — never a zero-byte file). The WAV
        is mono s16le at ``sample_rate`` (16 kHz for ``mic_in``, the TTS model
        rate for ``tts_out``). A filesystem failure is swallowed (logged) so a
        disk hiccup never crashes the finalize path — the turn row still lands.
        """

        if not pcm16:
            return None

        self._audio_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{turn_id}_{kind}_{uuid.uuid4().hex[:8]}.wav"
        path = self._audio_dir / filename
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(_WAV_CHANNELS)
                wav.setsampwidth(_WAV_SAMPLE_WIDTH_BYTES)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm16)
            byte_size = path.stat().st_size
        except OSError:
            _logger.exception("voice_store.blob_write_failed", turn_id=turn_id, kind=kind)
            return None

        created_at = _now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO voice_audio_blobs(turn_id, kind, path, bytes, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (turn_id, kind, str(path), byte_size, created_at),
            )
            blob_id = int(cursor.lastrowid or 0)
        return VoiceAudioBlobRow(
            id=blob_id,
            turn_id=turn_id,
            kind=kind,
            path=str(path),
            bytes=byte_size,
            created_at=created_at,
        )

    # -- reads (tests / retention) ------------------------------------------

    def get_turn(self, turn_id: str) -> VoiceTurnRow | None:
        """Return the ``voice_turns`` row for ``turn_id`` (or ``None``)."""

        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT turn_id, jarvis_msg_id, final_transcript, spoken_text,
                       started_at, ended_at, end_reason, draft_outcome, latency_json
                FROM voice_turns WHERE turn_id = ?
                """,
                (turn_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return VoiceTurnRow(*row)

    def list_turns(self) -> list[VoiceTurnRow]:
        """Return every ``voice_turns`` row, oldest insertion first."""

        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT turn_id, jarvis_msg_id, final_transcript, spoken_text,
                       started_at, ended_at, end_reason, draft_outcome, latency_json
                FROM voice_turns ORDER BY started_at ASC, turn_id ASC
                """
            )
            rows = cursor.fetchall()
        return [VoiceTurnRow(*row) for row in rows]

    def list_blobs(self, turn_id: str | None = None) -> list[VoiceAudioBlobRow]:
        """Return audio blob rows, oldest (lowest ``id``) first.

        ``turn_id`` filters to one turn; ``None`` returns every blob — the order
        the retention sweep evicts in.
        """

        with self._lock:
            if turn_id is None:
                cursor = self._conn.execute(
                    "SELECT id, turn_id, kind, path, bytes, created_at "
                    "FROM voice_audio_blobs ORDER BY id ASC"
                )
            else:
                cursor = self._conn.execute(
                    "SELECT id, turn_id, kind, path, bytes, created_at "
                    "FROM voice_audio_blobs WHERE turn_id = ? ORDER BY id ASC",
                    (turn_id,),
                )
            rows = cursor.fetchall()
        return [VoiceAudioBlobRow(*row) for row in rows]

    def total_audio_bytes(self) -> int:
        """Sum of ``bytes`` across every audio blob (the size-cap dimension)."""

        with self._lock:
            cursor = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM voice_audio_blobs")
            (total,) = cursor.fetchone()
        return int(total or 0)

    # -- deletes (retention) -------------------------------------------------

    def delete_blob(self, blob_id: int) -> None:
        """Delete one audio blob row by id (the file is removed by the caller).

        The retention policy owns the on-disk unlink so the delete order
        (file then row) is explicit there; this only drops the bookkeeping row.
        """

        with self._lock, self._conn:
            self._conn.execute("DELETE FROM voice_audio_blobs WHERE id = ?", (blob_id,))

    def delete_turn(self, turn_id: str) -> None:
        """Delete one ``voice_turns`` row by id (age-based eviction)."""

        with self._lock, self._conn:
            self._conn.execute("DELETE FROM voice_turns WHERE turn_id = ?", (turn_id,))


# --- Singleton plumbing -------------------------------------------------------
#
# The boot path in :mod:`bob.main` opens the SQLite connection, runs migrations
# and then calls :func:`set_default_store` with the wired store. The WS layer's
# persist hook resolves it via :func:`get_default_store`; test code and the
# attest backend do the same against a tmp DB / dir.

_DEFAULT_STORE: VoiceStore | None = None


def set_default_store(store: VoiceStore | None) -> None:
    """Install (or clear) the process-wide singleton :class:`VoiceStore`."""

    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def get_default_store() -> VoiceStore:
    """Return the process-wide singleton, raising if it hasn't been primed."""

    if _DEFAULT_STORE is None:
        raise RuntimeError(
            "VoiceStore default singleton not initialised. Did the app lifespan (bob.main) run?"
        )
    return _DEFAULT_STORE


__all__ = [
    "AUDIO_SUBDIR",
    "BlobKind",
    "EndReason",
    "VoiceAudioBlobRow",
    "VoiceStore",
    "VoiceTurnRow",
    "get_default_store",
    "set_default_store",
]
