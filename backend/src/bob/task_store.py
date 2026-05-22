"""SQLite-backed store for orchestrator sub-tasks and their message logs.

This is a deep module: callers only see :class:`TaskStore`, :class:`Task`,
:class:`TaskMessage`, and the small set of typed literals describing state /
role / action. All sqlite plumbing — connection handling, lock serialisation,
state-transition validation — lives behind those boundaries.

The schema lives in ``0002_tasks.sql`` (applied by the migration runner). The
boot path in :mod:`bob.main` opens the shared SQLite connection, applies
migrations, then primes the singleton via :func:`set_default_store`.

Threading model mirrors :class:`bob.jarvis_store.JarvisStore`: the underlying
connection is opened with ``check_same_thread=False`` upstream, and a
per-store :class:`threading.Lock` serialises writes so FastAPI request
workers cannot interleave statements.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Literal

TaskState = Literal["pending", "running", "waiting_input", "done", "failed"]
TaskRole = Literal["system", "user", "assistant", "tool"]
TaskAction = Literal["done", "ask_user", "progress"]


@dataclass(frozen=True)
class Task:
    """A single orchestrator sub-task row.

    ``needs_attention`` is the UI hint flag — set when the task is blocked
    waiting on the user. ``result`` is the final payload once ``state`` is
    ``done`` (or, optionally, ``failed``). Both nullable strings on the SQL
    side are surfaced as ``str | None``.
    """

    id: str
    title: str
    goal: str
    state: TaskState
    needs_attention: bool
    result: str | None
    parent_task_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskMessage:
    """One entry in a task's internal message log."""

    id: int
    task_id: str
    role: TaskRole
    content: str
    action: TaskAction | None
    created_at: str


# State machine for sub-tasks. ``done`` and ``failed`` are terminal — no
# outbound transitions. The orchestrator slice (#0018) is the primary caller
# but the rules live here so they are enforced at the data layer.
_VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"waiting_input", "done", "failed"}),
    "waiting_input": frozenset({"running", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
}


class TaskStoreError(RuntimeError):
    """Raised on data-layer violations: missing rows, invalid transitions, …"""


class TaskStore:
    """Persistent CRUD façade over the ``tasks`` and ``task_messages`` tables."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    # --- Task lifecycle ------------------------------------------------------

    def create_task(
        self,
        *,
        title: str,
        goal: str,
        parent_task_id: str | None = None,
    ) -> str:
        """Insert a new task in ``pending`` state and return its id."""

        task_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks(id, title, goal, state, needs_attention, parent_task_id)"
                " VALUES (?, ?, ?, 'pending', 0, ?)",
                (task_id, title, goal, parent_task_id),
            )
        return task_id

    def get_task(self, task_id: str) -> Task:
        """Return the task or raise :class:`TaskStoreError` if not found."""

        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, goal, state, needs_attention, result, parent_task_id,"
                " created_at, updated_at FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = cursor.fetchone()

        if row is None:
            raise TaskStoreError(f"task not found: {task_id}")
        return _row_to_task(row)

    def list_tasks(
        self,
        *,
        state: TaskState | None = None,
        limit: int | None = None,
    ) -> list[Task]:
        """Return tasks in creation order, optionally filtered by state."""

        query = (
            "SELECT id, title, goal, state, needs_attention, result, parent_task_id,"
            " created_at, updated_at FROM tasks"
        )
        params: list[object] = []
        if state is not None:
            query += " WHERE state = ?"
            params.append(state)
        # ``datetime('now')`` has second-precision so ties are likely in tests
        # and bursty boot-up flows. Break ties by sqlite's implicit ``rowid``
        # which monotonically reflects INSERT order — gives a deterministic
        # "creation order" guarantee callers can rely on.
        query += " ORDER BY created_at ASC, rowid ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        return [_row_to_task(row) for row in rows]

    def update_state(self, task_id: str, new_state: TaskState) -> None:
        """Validate the transition then persist ``new_state`` + bump ``updated_at``.

        Raises :class:`TaskStoreError` if the task doesn't exist or the
        ``current → new`` transition is not in :data:`_VALID_TRANSITIONS`.
        """

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "SELECT state FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise TaskStoreError(f"task not found: {task_id}")

            current: TaskState = row[0]
            if new_state not in _VALID_TRANSITIONS[current]:
                raise TaskStoreError(
                    f"invalid transition for task {task_id}: {current} -> {new_state}"
                )

            self._conn.execute(
                "UPDATE tasks SET state = ?, updated_at = datetime('now') WHERE id = ?",
                (new_state, task_id),
            )

    def set_needs_attention(self, task_id: str, needs_attention: bool) -> None:
        """Toggle the UI ``needs_attention`` hint on a task."""

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE tasks SET needs_attention = ?, updated_at = datetime('now') WHERE id = ?",
                (1 if needs_attention else 0, task_id),
            )
            if cursor.rowcount == 0:
                raise TaskStoreError(f"task not found: {task_id}")

    def set_result(self, task_id: str, result: str) -> None:
        """Store a task's final result payload. Does NOT change state.

        State transitions go through :meth:`update_state` so the orchestrator
        keeps full control over when (e.g.) ``done`` is recorded.
        """

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE tasks SET result = ?, updated_at = datetime('now') WHERE id = ?",
                (result, task_id),
            )
            if cursor.rowcount == 0:
                raise TaskStoreError(f"task not found: {task_id}")

    # --- Message log ---------------------------------------------------------

    def append_message(
        self,
        task_id: str,
        *,
        role: TaskRole,
        content: str,
        action: TaskAction | None = None,
    ) -> int:
        """Append a message to ``task_id``'s log; return the new row id."""

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO task_messages(task_id, role, content, action) VALUES (?, ?, ?, ?)",
                (task_id, role, content, action),
            )
            row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover — sqlite always sets lastrowid on INSERT
            raise TaskStoreError("INSERT into task_messages did not return a row id")
        return row_id

    def get_task_messages(self, task_id: str) -> list[TaskMessage]:
        """Return every message for ``task_id`` in chronological insert order."""

        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, task_id, role, content, action, created_at"
                " FROM task_messages WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            )
            rows = cursor.fetchall()
        return [
            TaskMessage(
                id=row[0],
                task_id=row[1],
                role=row[2],
                content=row[3],
                action=row[4],
                created_at=row[5],
            )
            for row in rows
        ]


def _row_to_task(row: tuple[object, ...]) -> Task:
    """Map a SELECT row to a :class:`Task` (centralises the cast set)."""

    (
        id_,
        title,
        goal,
        state,
        needs_attention,
        result,
        parent_task_id,
        created_at,
        updated_at,
    ) = row
    assert isinstance(id_, str)
    assert isinstance(title, str)
    assert isinstance(goal, str)
    assert isinstance(state, str)
    assert isinstance(needs_attention, int)
    assert result is None or isinstance(result, str)
    assert parent_task_id is None or isinstance(parent_task_id, str)
    assert isinstance(created_at, str)
    assert isinstance(updated_at, str)
    # ``state`` is constrained by the SQL CHECK to the TaskState set — the
    # cast to the Literal alias is safe.
    return Task(
        id=id_,
        title=title,
        goal=goal,
        state=state,  # type: ignore[arg-type]
        needs_attention=bool(needs_attention),
        result=result,
        parent_task_id=parent_task_id,
        created_at=created_at,
        updated_at=updated_at,
    )


# --- Singleton plumbing -------------------------------------------------------
#
# Mirrors :mod:`bob.jarvis_store`. The boot path (see :mod:`bob.main`) primes
# the singleton after migrations; tests prime it themselves when they need it.

_DEFAULT_STORE: TaskStore | None = None


def set_default_store(store: TaskStore | None) -> None:
    """Install (or clear) the process-wide singleton :class:`TaskStore`."""

    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def get_default_store() -> TaskStore:
    """Return the process-wide singleton, raising if it hasn't been primed."""

    if _DEFAULT_STORE is None:
        raise RuntimeError(
            "TaskStore default singleton not initialised. Did the app lifespan (bob.main) run?"
        )
    return _DEFAULT_STORE
