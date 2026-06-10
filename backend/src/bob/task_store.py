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

import json
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

#: Closed set of task states.
#:
#: Pre-PRD-0006 the runtime used ``pending`` / ``running`` / ``waiting_input``
#: / ``done`` / ``failed``. Issue 0050 (PRD 0006) ships the granular v2
#: lifecycle: ``spawned`` (replaces ``pending``), ``awaiting_input``
#: (renames ``waiting_input``) and the new ``superseded`` terminal value
#: for tasks replaced via ``replan_task``. The legacy literals stay in
#: the union so existing slice #0018 call sites (scheduler, runner)
#: keep compiling without a sweeping rename — the v2 tools use the new
#: names, and a future cleanup slice can collapse the union.
TaskState = Literal[
    "spawned",
    "pending",
    "running",
    "awaiting_input",
    "waiting_input",
    "done",
    "failed",
    "superseded",
]
TaskRole = Literal["system", "user", "assistant", "tool"]
TaskAction = Literal["done", "ask_user", "progress"]

#: Expected answer depth classified by Jarvis at spawn time (migration 0012).
#:
#: ``fact`` — a single fact / yes-no / number; the sub-agent stays minimal
#: and the done synthesis answers directly. ``brief`` — the default, pre-0012
#: behaviour. ``deep`` — full research + rich deliverable explicitly asked.
TaskScope = Literal["fact", "brief", "deep"]

#: Runtime fallback when the column carries an unknown value (defensive read).
DEFAULT_TASK_SCOPE: TaskScope = "brief"

_VALID_SCOPES: frozenset[str] = frozenset({"fact", "brief", "deep"})

#: How sure the sub-agent is of its terminal answer (migration 0013).
#: ``confirmed`` — cross-checked / directly stated by a reliable source.
#: ``probable`` — best available answer, NOT verified (fast fact-scope
#: answer, iteration-cap exit). ``None`` (legacy rows / model omitted the
#: field) reads as ``confirmed`` so pre-0013 behaviour is unchanged.
TaskConfidence = Literal["confirmed", "probable"]

_VALID_CONFIDENCES: frozenset[str] = frozenset({"confirmed", "probable"})


@dataclass(frozen=True)
class Task:
    """A single orchestrator sub-task row.

    ``needs_attention`` is the UI hint flag — set when the task is blocked
    waiting on the user. ``result`` is the final payload once ``state`` is
    ``done`` (or, optionally, ``failed``). Both nullable strings on the SQL
    side are surfaced as ``str | None``. ``dismissed`` is the slice #0024
    "user hid this row from the sidebar" flag — preserved in SQLite so
    history stays intact while the live sidebar can be cleaned.

    ``lineage`` (PRD 0006 / issue 0044) is the ordered list of task ids that
    a future ``replan_task`` tool would chain together: when a task is
    replanned, the new task is created with the previous chain ([old_id, …])
    so the audit trail survives across cancel/respawn cycles. Empty list for
    tasks created by the v1 (today's) flow.
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
    dismissed: bool
    #: Expected answer depth (migration 0012) — see :data:`TaskScope`.
    #: Drives the sub-agent prompt directive and the done-synthesis template.
    scope: TaskScope = DEFAULT_TASK_SCOPE
    #: Structured deliverable persisted alongside the spoken / markdown
    #: ``result`` text (PRD 0008 / issue 0064; PRD 0010 / issue 0066 made it a
    #: LIST). Holds a list of ``{"component": ..., "props": {...}}`` section
    #: descriptors the sub-agent's terminal deliverable resolved to, so the
    #: frontend can reconstruct the matching sections overlay (Markdown, Mail,
    #: future surfaces) on the completion event and on recall. The empty list
    #: means "no structured deliverable" (summary-only / cap paths) — the
    #: ``result`` text remains the rendering source in that case. Decoding is
    #: defensive: any legacy single-object / corrupt / ``null`` column value is
    #: read back as the empty list and never raises (issue 0066).
    result_payload: list[dict[str, object]] = field(default_factory=list)
    #: Confidence of the terminal answer (migration 0013) — see
    #: :data:`TaskConfidence`. ``None`` reads as ``confirmed`` (legacy).
    confidence: TaskConfidence | None = None
    lineage: list[str] = field(default_factory=list)
    #: Turn index at which Jarvis delivered this task's result to the user.
    #:
    #: PRD 0006 / issue 0050 — guards against announcing the same
    #: completion twice. ``None`` while the task is still active or
    #: queued for delivery; set to the user-turn index by the
    #: orchestrator once the spoken acknowledgement has been emitted.
    delivered_at_turn: int | None = None


@dataclass(frozen=True)
class TaskMessage:
    """One entry in a task's internal message log."""

    id: int
    task_id: str
    role: TaskRole
    content: str
    action: TaskAction | None
    created_at: str


# State machine for sub-tasks. ``done`` / ``failed`` / ``superseded`` are
# terminal. The orchestrator slice (#0018) is the primary caller but the
# rules live here so they are enforced at the data layer.
#
# PRD 0006 / issue 0050 adds ``spawned`` (alias of pre-existing
# ``pending``), ``awaiting_input`` (rename of ``waiting_input``) and the
# new ``superseded`` terminal state. Both legacy aliases remain in the
# transition table so the scheduler / runner code paths that still emit
# them keep working — the v2 tools use the new names.
_VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    # ``spawned`` and ``pending`` are interchangeable queued states
    # (slice #0018 legacy vs PRD 0006 v2). They round-trip so the
    # v2 tools can normalise a freshly-created row to ``spawned``
    # without being blocked by a tie to the SQL default.
    "spawned": frozenset({"running", "pending", "failed", "superseded"}),
    "pending": frozenset({"running", "spawned", "failed", "superseded"}),
    "running": frozenset({"awaiting_input", "waiting_input", "done", "failed", "superseded"}),
    "awaiting_input": frozenset({"running", "failed", "superseded"}),
    "waiting_input": frozenset({"running", "failed", "superseded"}),
    "done": frozenset(),
    # ``failed`` rows can be lifted to ``superseded`` by
    # :meth:`mark_superseded` so the v2 replan flow (cancel via the
    # scheduler → row flipped to failed → mark superseded) ends with
    # the audit-correct terminal state without bypassing the
    # transition validator.
    "failed": frozenset({"superseded"}),
    "superseded": frozenset(),
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
        lineage: Sequence[str] | None = None,
        scope: TaskScope = DEFAULT_TASK_SCOPE,
    ) -> str:
        """Insert a new task in ``pending`` state and return its id.

        ``lineage`` (PRD 0006 / issue 0044) defaults to an empty list. A
        future ``replan_task`` tool will pass the previous task chain when
        spawning the replacement. ``scope`` (migration 0012) records the
        expected answer depth classified by Jarvis at spawn time.
        """

        task_id = uuid.uuid4().hex
        lineage_json = json.dumps(list(lineage) if lineage else [])
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks"
                "(id, title, goal, state, needs_attention, parent_task_id, lineage, scope)"
                " VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)",
                (task_id, title, goal, parent_task_id, lineage_json, scope),
            )
        return task_id

    def get_task(self, task_id: str) -> Task:
        """Return the task or raise :class:`TaskStoreError` if not found.

        Dismissed tasks are still returned — the dismissal only affects the
        :meth:`list_tasks` replay path, not individual lookups (the drawer
        needs to render dismissed tasks if directly addressed).
        """

        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, goal, state, needs_attention, result, result_payload,"
                " parent_task_id, created_at, updated_at, dismissed, lineage,"
                " delivered_at_turn, scope, confidence"
                " FROM tasks WHERE id = ?",
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
        include_dismissed: bool = False,
    ) -> list[Task]:
        """Return tasks in creation order, optionally filtered by state.

        By default dismissed tasks are excluded so the sidebar replay path
        stays clean. Pass ``include_dismissed=True`` to get the full set
        (debug / admin paths).
        """

        query = (
            "SELECT id, title, goal, state, needs_attention, result, result_payload,"
            " parent_task_id, created_at, updated_at, dismissed, lineage, delivered_at_turn,"
            " scope, confidence"
            " FROM tasks"
        )
        where: list[str] = []
        params: list[object] = []
        if state is not None:
            where.append("state = ?")
            params.append(state)
        if not include_dismissed:
            where.append("dismissed = 0")
        if where:
            query += " WHERE " + " AND ".join(where)
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

    def find_by_query(
        self,
        query: str,
        *,
        prefer_state: TaskState | None = None,
        limit: int = 1,
    ) -> list[Task]:
        """Fuzzy-match tasks against ``query`` on ``title`` + ``goal``.

        Splits ``query`` on whitespace and requires every token to appear
        (case-insensitively) somewhere in ``title || ' ' || goal``. Ranks
        ``prefer_state`` first (e.g. ``"done"`` for "ressortir un livrable"
        flows), then by ``delivered_at_turn`` descending (delivered tasks
        first), then by ``created_at`` descending (recent first). Dismissed
        rows are excluded — the result feeds Jarvis tool calls that surface
        a user-visible deliverable, and dismissed cards were explicitly
        hidden by the user.

        Returns an empty list when ``query`` is blank or no row matches.
        """

        tokens = [t.strip().lower() for t in query.split() if t.strip()]
        if not tokens:
            return []

        # Basic LIKE escaping so a raw ``%`` / ``_`` / ``\`` in the query
        # behaves as a literal. The ESCAPE clause below activates the
        # escape character. Single-user desktop scope so SQL-injection is
        # not the threat model — we just want predictable matches.
        def _escape_like(token: str) -> str:
            return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        where_clauses = ["LOWER(title || ' ' || goal) LIKE ? ESCAPE '\\'" for _ in tokens]
        where_clauses.append("dismissed = 0")
        params: list[object] = [f"%{_escape_like(t)}%" for t in tokens]

        order_parts: list[str] = []
        if prefer_state is not None:
            order_parts.append("CASE WHEN state = ? THEN 0 ELSE 1 END")
            params.append(prefer_state)
        # Treat NULL ``delivered_at_turn`` as the lowest so already-delivered
        # rows outrank queued-completion rows that never reached the user.
        order_parts.append("COALESCE(delivered_at_turn, -1) DESC")
        order_parts.append("created_at DESC")
        order_parts.append("rowid DESC")

        sql = (
            "SELECT id, title, goal, state, needs_attention, result, result_payload,"
            " parent_task_id, created_at, updated_at, dismissed, lineage, delivered_at_turn,"
            " scope, confidence"
            " FROM tasks"
            f" WHERE {' AND '.join(where_clauses)}"
            f" ORDER BY {', '.join(order_parts)}"
            " LIMIT ?"
        )
        params.append(limit)

        with self._lock:
            cursor = self._conn.execute(sql, params)
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

    def set_result(
        self,
        task_id: str,
        result: str,
        *,
        result_payload: list[dict[str, object]] | None = None,
        confidence: TaskConfidence | None = None,
    ) -> None:
        """Store a task's final result text + optional list of section descriptors.

        ``confidence`` (migration 0013) records how sure the sub-agent is of
        the answer — ``None`` (default) leaves the column NULL, read back as
        ``confirmed``. The done-synthesis uses ``probable`` to voice the
        uncertainty and offer a deeper follow-up run.

        ``result`` is the spoken / markdown text (the source of truth for the
        ``task_result`` WS string and ``show_task_result`` recall). PRD 0008 /
        issue 0064 added a structured ``result_payload``; PRD 0010 / issue 0066
        made it a **list** of ``{"component": ..., "props": {...}}`` section
        descriptors (a single card is a list-of-one). When the sub-agent's
        terminal deliverable resolved to a non-empty list the runner passes it
        here so the sections survive to the frontend and rebuild the matching
        overlay (Markdown, Mail, …). Pass ``None`` or an empty list (the
        default) for summary-only / cap results — the column is stored as a
        ``NULL`` so a stale descriptor never lingers.

        Does NOT change state — transitions go through :meth:`update_state` so
        the orchestrator keeps full control over when (e.g.) ``done`` is
        recorded.
        """

        payload_json = json.dumps(result_payload) if result_payload else None
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE tasks SET result = ?, result_payload = ?, confidence = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (result, payload_json, confidence, task_id),
            )
            if cursor.rowcount == 0:
                raise TaskStoreError(f"task not found: {task_id}")

    def set_delivered_at_turn(self, task_id: str, turn_index: int) -> None:
        """Stamp ``task_id`` as delivered at user-turn index ``turn_index``.

        PRD 0006 / issue 0050. Set once Jarvis has spoken the completion
        announcement; the :class:`StateBlockProvider` then keeps the task
        visible for ``recent_turns_for_done_inclusion`` user turns before
        eviction so the next user reply can still address the result by
        natural reference.

        Subsequent calls overwrite the value: ``replan_task`` reuses the
        delivery slot. ``turn_index`` is a non-negative monotonic
        identifier shared with the orchestrator's turn counter.
        """

        if turn_index < 0:
            raise TaskStoreError(f"delivered_at_turn must be >= 0, got {turn_index}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE tasks SET delivered_at_turn = ?, updated_at = datetime('now') WHERE id = ?",
                (turn_index, task_id),
            )
            if cursor.rowcount == 0:
                raise TaskStoreError(f"task not found: {task_id}")

    def mark_superseded(self, task_id: str) -> None:
        """Force-transition ``task_id`` to the ``superseded`` terminal state.

        PRD 0006 / issue 0050. Used by ``replan_task`` after the previous
        task has been cancelled: the cancel finalises the row in
        ``failed``, then ``mark_superseded`` flips it to the v2 terminal
        ``superseded`` so the audit trail distinguishes "the user changed
        their mind" from "the runner crashed". The transition is
        validated through :data:`_VALID_TRANSITIONS` so a real terminal
        (already ``done`` / ``superseded``) raises.
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
            if "superseded" not in _VALID_TRANSITIONS[current]:
                raise TaskStoreError(
                    f"invalid transition for task {task_id}: {current} -> superseded"
                )
            self._conn.execute(
                "UPDATE tasks SET state = 'superseded', updated_at = datetime('now') WHERE id = ?",
                (task_id,),
            )

    def dismiss_task(self, task_id: str) -> None:
        """Flip ``dismissed`` to true so the task hides from sidebar replays.

        The row stays in SQLite — :meth:`get_task` still returns it and the
        drawer can still render it if the frontend addresses it directly.
        Raises :class:`TaskStoreError` when the task does not exist. No
        state-transition validation: dismiss is orthogonal to the lifecycle
        (the UI only exposes the button for ``done`` / ``failed`` cards, but
        the data layer stays permissive).
        """

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE tasks SET dismissed = 1, updated_at = datetime('now') WHERE id = ?",
                (task_id,),
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
        result_payload_raw,
        parent_task_id,
        created_at,
        updated_at,
        dismissed,
        lineage_raw,
        delivered_at_turn,
        scope_raw,
        confidence_raw,
    ) = row
    assert isinstance(id_, str)
    assert isinstance(title, str)
    assert isinstance(goal, str)
    assert isinstance(state, str)
    assert isinstance(needs_attention, int)
    assert result is None or isinstance(result, str)
    assert result_payload_raw is None or isinstance(result_payload_raw, str)
    assert parent_task_id is None or isinstance(parent_task_id, str)
    assert isinstance(created_at, str)
    assert isinstance(updated_at, str)
    assert isinstance(dismissed, int)
    assert isinstance(lineage_raw, str)
    assert delivered_at_turn is None or isinstance(delivered_at_turn, int)
    assert isinstance(scope_raw, str)
    lineage = _decode_lineage(lineage_raw)
    result_payload = _decode_result_payload(result_payload_raw)
    # Defensive read: collapse any unknown / legacy value to the default —
    # scope is a prompt-shaping hint, never load-bearing for execution.
    scope: TaskScope = (
        scope_raw if scope_raw in _VALID_SCOPES else DEFAULT_TASK_SCOPE  # type: ignore[assignment]
    )
    # Defensive read: any unknown / legacy value collapses to None (read as
    # ``confirmed``) — confidence shapes the announcement, never execution.
    confidence: TaskConfidence | None = (
        confidence_raw if confidence_raw in _VALID_CONFIDENCES else None  # type: ignore[assignment]
    )
    # ``state`` is constrained by the SQL CHECK to the TaskState set — the
    # cast to the Literal alias is safe.
    return Task(
        id=id_,
        title=title,
        goal=goal,
        state=state,  # type: ignore[arg-type]
        needs_attention=bool(needs_attention),
        result=result,
        result_payload=result_payload,
        parent_task_id=parent_task_id,
        created_at=created_at,
        updated_at=updated_at,
        dismissed=bool(dismissed),
        scope=scope,
        confidence=confidence,
        lineage=lineage,
        delivered_at_turn=delivered_at_turn,
    )


def _decode_lineage(raw: str) -> list[str]:
    """Decode the ``lineage`` JSON-text column into a ``list[str]``.

    The migration in ``0005_tasks_lineage.sql`` defaults the column to
    ``'[]'`` so any row stored before the migration is consistent. We still
    guard against legacy / corrupted values by collapsing to ``[]`` —
    lineage is metadata, never load-bearing for the task-execution path.
    """

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def _decode_result_payload(raw: str | None) -> list[dict[str, object]]:
    """Decode the ``result_payload`` JSON-text column into a list of descriptors.

    PRD 0008 / issue 0064 introduced the column; PRD 0010 / issue 0066 made the
    contract a **list** of ``{"component": ..., "props": {...}}`` section
    descriptors. Decoding is DEFENSIVE — it is an invariant, never a back-fill:

    - ``NULL`` (pre-0064 rows, summary-only tasks) → ``[]``;
    - a JSON array → the list, keeping only dict items (a stray non-object
      element is dropped, never crashes the render);
    - a legacy single object (``{"component": ...}`` written by issue 0064
      before this change), ``null``, a number/string, or corrupt JSON → ``[]``.

    It NEVER raises and NEVER returns a non-list — old rows are not migrated,
    just rendered harmless. The descriptor list is a rendering hint, never
    load-bearing for the task-execution path; the spoken ``result`` string
    always survives independently.
    """

    if raw is None:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, dict)]


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
