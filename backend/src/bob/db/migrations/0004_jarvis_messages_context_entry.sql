-- 0004 — Add ContextEntry columns to ``jarvis_messages``.
--
-- Foundation for PRD 0006 (Jarvis v2 Context Overhaul) / issue 0043. We
-- extend the existing ``jarvis_messages`` table in-place so old rows survive
-- the migration verbatim and the legacy full-history provider keeps reading
-- them by ``id`` order.
--
-- Idempotency: SQLite does not natively support ``ADD COLUMN IF NOT EXISTS``,
-- but ``ALTER TABLE ... ADD COLUMN`` is wrapped in this migration file. The
-- migration runner (``bob.db.migrations_runner``) tracks applied filenames
-- in the ``_migrations`` bookkeeping table, so re-running is a no-op at the
-- runner level. The columns therefore are only ever added once.
--
-- Down migration (manual, for documentation):
--   DELETE FROM _migrations WHERE filename = '0004_jarvis_messages_context_entry.sql';
--   -- and rebuild jarvis_messages without these columns via the standard
--   -- sqlite ``CREATE TABLE jarvis_messages_old; INSERT INTO …; DROP TABLE; …``
--   -- dance. We never expect to do this in production — sqlite columns are
--   -- cheap to leave around.

-- New ContextEntry fields on the existing jarvis_messages table.
-- ``schema_version`` defaults to 1 so backfill is trivial.
-- ``kind`` is backfilled from ``role`` in the body of this migration below.
-- ``source`` is set to ``"jarvis_store"`` for every legacy row.
-- ``payload`` is JSON-encoded ({"role": role, "content": content}) so the
-- legacy provider can reconstitute the chat message without re-querying.
ALTER TABLE jarvis_messages ADD COLUMN kind TEXT;
ALTER TABLE jarvis_messages ADD COLUMN source TEXT;
ALTER TABLE jarvis_messages ADD COLUMN token_estimate INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jarvis_messages ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jarvis_messages ADD COLUMN provider_id TEXT;
ALTER TABLE jarvis_messages ADD COLUMN payload TEXT;
ALTER TABLE jarvis_messages ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1;

-- Backfill ContextEntry fields for every existing row.
-- ``kind`` derives from ``role``: user → user_turn, assistant → assistant_turn,
-- everything else collapses to ``system_note``.
UPDATE jarvis_messages
SET kind = CASE role
    WHEN 'user' THEN 'user_turn'
    WHEN 'assistant' THEN 'assistant_turn'
    ELSE 'system_note'
END
WHERE kind IS NULL;

UPDATE jarvis_messages SET source = 'jarvis_store' WHERE source IS NULL;
UPDATE jarvis_messages SET provider_id = 'legacy_full_history' WHERE provider_id IS NULL;

-- ``token_estimate`` defaults to 0 in the column declaration above; refresh
-- it to a rough ``len(content) / 4`` so the field carries a real signal for
-- rows that existed before issue 0043 introduced the field. Integer
-- division in sqlite is ``/``; we floor with ``CAST(length(content) / 4 AS INTEGER)``.
UPDATE jarvis_messages
SET token_estimate = CAST(length(content) / 4 AS INTEGER)
WHERE token_estimate = 0 AND content IS NOT NULL;

-- ``payload`` carries the role+content as a JSON object so future
-- providers / migrations can read it without re-parsing ``role`` and
-- ``content`` separately. Use ``json_object`` for safe escaping.
UPDATE jarvis_messages
SET payload = json_object('role', role, 'content', content)
WHERE payload IS NULL;
