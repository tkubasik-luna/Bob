-- 0007 — Epoch sealing + cross-epoch digest tables (PRD 0006 / issue 0051).
--
-- Adds an ``epoch_id`` column on every ``ContextEntry``-bearing table so the
-- orchestrator can keep "current epoch" recent turns + rolling summary
-- separate from older sealed material. Sealed epochs stay in SQLite but
-- are NEVER auto-injected — only the cross-epoch digest is.
--
-- Two changes:
--
--   1) ``jarvis_messages.epoch_id`` and ``rolling_summaries.epoch_id``
--      columns, default ``0`` so existing rows backfill into the implicit
--      "epoch 0".
--   2) ``cross_epoch_digests`` table: append-only digest summary rebuilt
--      from RAW sealed turns at every new seal (never from prior digests
--      — bounded drift). Each row stamps ``summariser_version`` and the
--      ``sealed_epoch_count`` it folded in.
--
-- The "sealed rolling summary" itself is not a new table — a sealed epoch
-- is just the freshest ``rolling_summaries`` row whose ``epoch_id`` matches.
-- The :class:`bob.epoch.manager.EpochManager` flips the current epoch id
-- forward on seal; the prior summary stays in place (immutable + queryable
-- by the retrieval stub later).
--
-- Idempotency: ``apply_migrations`` records filenames in ``_migrations`` so
-- re-running is a no-op at the runner level. ``CREATE TABLE IF NOT EXISTS``
-- + ``ALTER TABLE … ADD COLUMN`` only execute once per fresh DB.
--
-- Down migration (manual, documentation only):
--   DELETE FROM _migrations WHERE filename = '0007_epoch_id_columns.sql';
--   DROP TABLE cross_epoch_digests;
--   -- ALTER TABLE … DROP COLUMN epoch_id; (sqlite ≥3.35) on both tables.
--   -- Pre-3.35 sqlite requires the standard ``CREATE TABLE _old; INSERT…;
--   -- DROP; RENAME`` dance for column removal. We never expect to do
--   -- this in production — sqlite columns are cheap to leave around.

ALTER TABLE jarvis_messages ADD COLUMN epoch_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rolling_summaries ADD COLUMN epoch_id INTEGER NOT NULL DEFAULT 0;

-- Backfill is implicit via DEFAULT 0, but make the intent explicit so the
-- migration is auditable even if a future review reads only the body.
UPDATE jarvis_messages SET epoch_id = 0 WHERE epoch_id IS NULL;
UPDATE rolling_summaries SET epoch_id = 0 WHERE epoch_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_jarvis_messages_epoch_id
    ON jarvis_messages(epoch_id, id);

CREATE INDEX IF NOT EXISTS idx_rolling_summaries_epoch_id
    ON rolling_summaries(epoch_id, id DESC);

-- Cross-epoch digest table — append-only. Each new seal inserts a fresh
-- row rebuilt from RAW sealed turns. The freshest row (highest id) is the
-- one injected into the active prompt by :class:`CrossEpochDigestProvider`.
CREATE TABLE IF NOT EXISTS cross_epoch_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    summariser_version INTEGER NOT NULL,
    sealed_epoch_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cross_epoch_digests_recent
    ON cross_epoch_digests(id DESC);
