-- 0010 — Add ``voice_turns`` for the full-duplex voice persistence (PRD 0016 /
-- issue 0109, Annexe E.1).
--
-- Background: a real-time voice turn finalizes in the full-duplex loop
-- (:mod:`bob.voice_loop`) on one of four end reasons — ``completed`` (the
-- say-path streamed Bob's whole reply), ``bargein`` (the user cut Bob off),
-- ``voice_stop`` (the toggle went OFF / socket closed mid-turn) or ``error``
-- (STT/finalize failed cleanly, Annexe G). At that moment we persist ONE row
-- here so the turn can be replayed / tuned offline without keeping audio in
-- RAM.
--
-- Column notes (Annexe E.1):
--   * ``turn_id``          — the loop's hex turn id; primary key (one row/turn).
--   * ``jarvis_msg_id``    — links the final transcript to the Jarvis thread
--                            entry it became (``jarvis_messages``); NULL until the
--                            transcript is committed to history.
--   * ``final_transcript`` — the frozen ``stt_final`` text (what the user said).
--   * ``spoken_text``      — what Bob ACTUALLY played (post-barge-in this is the
--                            committed prefix, not the full reply).
--   * ``started_at`` / ``ended_at`` — ISO-8601 UTC wall-clock turn bounds.
--   * ``end_reason``       — 'completed' | 'bargein' | 'voice_stop' | 'error'.
--   * ``draft_outcome``    — 'committed' | 'discarded' | 'none' (speculative
--                            Draft, issue 0104); 'none' until that slice lands.
--   * ``latency_json``     — the Annexe F ``turn_latency`` marks + derived,
--                            serialised as JSON text (NULL when no marks).
--
-- Retention (:class:`bob.voice_retention_policy.VoiceRetentionPolicy`): this
-- table is bounded by AGE (default 30 days), oldest first. The audio blobs in
-- ``voice_audio_blobs`` (migration 0011) are bounded SEPARATELY by total SIZE.
--
-- Idempotency: gated by the runner (:mod:`bob.db.migrations_runner`) via the
-- ``_migrations`` bookkeeping row, so re-running is a no-op.
--
-- Down migration (manual, documentation only):
--   DELETE FROM _migrations WHERE filename = '0010_voice_turns.sql';
--   DROP TABLE voice_turns;

CREATE TABLE voice_turns (
    turn_id          TEXT PRIMARY KEY,
    jarvis_msg_id    TEXT,
    final_transcript TEXT,
    spoken_text      TEXT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    end_reason       TEXT,
    draft_outcome    TEXT,
    latency_json     TEXT
);
