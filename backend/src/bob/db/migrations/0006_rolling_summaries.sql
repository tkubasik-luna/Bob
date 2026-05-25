-- 0006 — Persistent rolling-summary store for PRD 0006 / issue 0046.
--
-- The bounded ``ContextPolicy`` keeps Jarvis context flat by replacing
-- pre-2024 "send the whole thread every turn" behavior with a rolling
-- summary block over older turns plus a verbatim recent-turns window.
-- Every regeneration of the rolling summary is performed against RAW
-- older turns (never the prior digest, to bound drift) and persists the
-- ``summariser_version`` so a future wording change is visible at the
-- data layer.
--
-- Schema:
--
-- * ``id`` — primary key, autoincrement.
-- * ``from_turn`` / ``to_turn`` — 1-indexed, inclusive bounds over the
--   user↔assistant turn pairs folded into the summary at generation time.
-- * ``summariser_version`` — stamp of the summariser revision that
--   produced this row (see ``bob.context.summariser.SUMMARISER_VERSION``).
-- * ``text`` — the rendered summary string.
-- * ``token_estimate`` — rough budget signal for the assembler.
-- * ``created_at`` — ISO timestamp, autopopulated.
--
-- The schema is append-only: each regeneration inserts a new row. The
-- ``RollingSummaryProvider`` picks the freshest row at assembly time. We
-- intentionally do not DELETE old summaries — they are tiny, auditable,
-- and useful when debugging drift.
--
-- Idempotency: the migration runner (``bob.db.migrations_runner``) tracks
-- applied filenames in ``_migrations`` so re-running the runner is a
-- no-op at the file level. ``CREATE TABLE IF NOT EXISTS`` keeps the body
-- safe under ad-hoc replays.
--
-- Down migration (manual, documentation only):
--   DELETE FROM _migrations WHERE filename = '0006_rolling_summaries.sql';
--   DROP TABLE rolling_summaries;

CREATE TABLE IF NOT EXISTS rolling_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_turn INTEGER NOT NULL,
    to_turn INTEGER NOT NULL,
    summariser_version INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rolling_summaries_to_turn
    ON rolling_summaries(to_turn DESC, id DESC);
