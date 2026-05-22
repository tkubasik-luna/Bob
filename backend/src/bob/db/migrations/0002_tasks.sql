-- 0002 — Orchestrator sub-tasks + their internal message logs.
--
-- ``tasks`` rows represent units of work spawned by the Jarvis orchestrator
-- (e.g. "research X", "draft Y"). Each task carries its own message history
-- in ``task_messages`` — separate from the singleton Jarvis thread defined
-- in 0001, so a task's internal back-and-forth doesn't pollute the main
-- conversation.
--
-- ``parent_task_id`` is reserved for future task-spawns-task scenarios;
-- nullable today, FK to ``tasks(id)`` so cascades stay enforceable.
-- ``state`` mirrors the TaskStore Python literal; the CHECK constraint is
-- defence-in-depth alongside the application-level transition validator.
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','running','waiting_input','done','failed')),
    needs_attention INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    parent_task_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);

CREATE TABLE IF NOT EXISTS task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content TEXT NOT NULL,
    action TEXT CHECK (action IN ('done','ask_user','progress')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_messages_task_id_created ON task_messages(task_id, created_at);
