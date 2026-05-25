-- 0008 — Refine ``tasks.state`` to the v2 lifecycle (PRD 0006 / issue 0050).
--
-- Pre-0050 the column allowed ``pending``/``running``/``waiting_input``/
-- ``done``/``failed``. The v2 model maps the slice #0018 set onto a
-- granular lifecycle:
--
--   - ``spawned``        — newly created, scheduler has not promoted yet
--                          (replaces ``pending``).
--   - ``running``        — actively executing under the cap.
--   - ``awaiting_input`` — paused for a user reply (renamed from
--                          ``waiting_input`` to match the PRD wording).
--   - ``done``           — terminal success.
--   - ``failed``         — terminal failure (cancel / timeout / error).
--   - ``superseded``     — replaced by a fresh task via ``replan_task``
--                          (new value introduced here).
--
-- SQLite cannot ALTER a CHECK constraint in place, so we rebuild the
-- table (copying rows verbatim then renaming) — the canonical SQLite
-- pattern. To stay compatible with running code (slice #0018 task store
-- still uses ``pending`` and ``waiting_input``) the migration ALSO
-- accepts the legacy literals so existing rows pass the new CHECK
-- without translation.
--
-- Idempotency: gated by the runner via ``_migrations`` row.

PRAGMA foreign_keys = OFF;

CREATE TABLE tasks_new (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'spawned', 'pending',
        'running',
        'awaiting_input', 'waiting_input',
        'done',
        'failed',
        'superseded'
    )),
    needs_attention INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    parent_task_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    dismissed INTEGER NOT NULL DEFAULT 0,
    lineage TEXT NOT NULL DEFAULT '[]',
    delivered_at_turn INTEGER,
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);

INSERT INTO tasks_new (
    id, title, goal, state, needs_attention, result, parent_task_id,
    created_at, updated_at, dismissed, lineage, delivered_at_turn
)
SELECT
    id, title, goal, state, needs_attention, result, parent_task_id,
    created_at, updated_at, dismissed, lineage, NULL
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_delivered_at_turn ON tasks(delivered_at_turn);

PRAGMA foreign_keys = ON;
