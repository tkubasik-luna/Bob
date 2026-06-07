-- 0011 — Add ``voice_audio_blobs`` for the full-duplex voice persistence (PRD
-- 0016 / issue 0109, Annexe E.2).
--
-- Background: a finalized voice turn (``voice_turns``, migration 0010) carries
-- up to two audio recordings — the user's mic input (``mic_in``) and Bob's
-- spoken reply (``tts_out``). The raw PCM rides the WS in real time and would
-- bloat the SQLite DB if inlined; instead each recording is written as a WAV
-- file on disk under ``{BOB_DATA_DIR}/voice_audio/`` and only its filesystem
-- ``path`` (+ byte size) is recorded here.
--
-- Column notes (Annexe E.2):
--   * ``id``         — surrogate autoincrement PK; the eviction order key
--                      (lowest id == oldest blob).
--   * ``turn_id``    — the ``voice_turns`` row this blob belongs to (not a hard
--                      FK so a turn-row purge by AGE and a blob purge by SIZE
--                      stay independent — Annexe E.3 separate caps).
--   * ``kind``       — 'mic_in' | 'tts_out'.
--   * ``path``       — absolute path to the WAV file on disk (NOT the bytes).
--   * ``bytes``      — size of the WAV file, summed by the retention policy.
--   * ``created_at`` — ISO-8601 UTC write time.
--
-- Retention (:class:`bob.voice_retention_policy.VoiceRetentionPolicy`): this
-- table is bounded by total SIZE (sum of ``bytes``, default 1.5 GiB), oldest
-- (lowest ``id``) first, deleting BOTH the file on disk AND the row. The text
-- rows in ``voice_turns`` are bounded SEPARATELY by age.
--
-- Idempotency: gated by the runner (:mod:`bob.db.migrations_runner`) via the
-- ``_migrations`` bookkeeping row, so re-running is a no-op.
--
-- Down migration (manual, documentation only):
--   DELETE FROM _migrations WHERE filename = '0011_voice_audio_blobs.sql';
--   DROP TABLE voice_audio_blobs;

CREATE TABLE voice_audio_blobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
