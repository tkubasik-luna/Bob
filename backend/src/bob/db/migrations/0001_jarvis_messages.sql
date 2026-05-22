-- 0001 — Jarvis main thread messages.
--
-- Single-thread (no session_id / task_id columns): Bob is a desktop solo
-- assistant with one persistent Jarvis conversation that survives restarts.
-- ``action`` is nullable today, reserved for sub-agent integration in later
-- slices (``done`` / ``ask_user`` / ``progress``).
CREATE TABLE IF NOT EXISTS jarvis_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content TEXT NOT NULL,
    action TEXT CHECK (action IN ('done','ask_user','progress')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jarvis_messages_created ON jarvis_messages(created_at);
