"""StateBlockProvider — emit a STATE summary entry for every active task.

PRD 0006 / issue 0050. Bounded context already gives Jarvis a small
window of verbatim recent turns plus a rolling summary; the STATE
block adds the third leg of the bounded prompt — a structured list of
*currently relevant* sub-tasks. The provider answers two questions at
once:

1. "Which ``task_id`` should I reference if the user says *'annule
   ça'*?" — the active set is rendered with explicit ids so Jarvis can
   round-trip the ``cancel_task`` / ``addendum_task`` / ``replan_task``
   tools without fuzzy matching.
2. "Is the user still on topic, or have we moved on?" — every entry
   carries a structured ``recency`` ``{"active", "stale"}`` label so
   the prompt template can pick the matching delivery phrasing.

Cardinality / shape (PRD verbatim):

* Active set = (not-done tasks) UNION (terminal tasks within the last
  K user turns AND ``delivered_at_turn`` set). K is from
  :attr:`StatePolicy.recent_turns_for_done_inclusion`.
* Per-row fields: ``id``, ``title`` (≤ 8 words), ``state``,
  ``last_update_1liner`` (≤ 120 chars), ``delivered_at_turn``,
  ``last_event_id`` (provenance), ``lineage``, and the *recomputed*
  ``age_min`` + ``recency`` signals.
* ``age_min`` is always recomputed at assembly time — never read from
  persistence. The PRD is loud about this so a stale persisted value
  can never leak.
* Hard caps via :class:`StatePolicy`: per-field char limits + max
  entry count. Eviction order is delegated to an injectable
  :class:`EvictionStrategy` so future tuning is a wiring change.
* Token budget is asserted in tests, not enforced at runtime.

The block is emitted as a single ``role=system`` :class:`ContextEntry`
whose payload carries the rendered text. The Jarvis prompt template
references the ``recency`` field per-line so the LLM picks "Voilà X"
(active) vs "Tu m'avais demandé X, voilà…" (stale) phrasing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry
from bob.context.eviction import (
    DefaultEvictionStrategy,
    EvictionStrategy,
    StateBlockCandidate,
)
from bob.context.provider import AssemblyContext
from bob.context.recency import (
    RecencyPolicy,
    RecencySignal,
    classify_recency,
    default_recency_policy,
)
from bob.context.state_policy import StatePolicy, default_state_policy
from bob.task_store import Task, TaskStore, TaskStoreError

#: Stable provider id used by the assembler registry.
STATE_BLOCK_PROVIDER_ID = "state_block"


#: States that ALWAYS qualify a task as "active" regardless of recency.
_LIVE_STATES = frozenset(
    {
        "running",
        "spawned",
        "pending",
        "awaiting_input",
        "waiting_input",
    }
)


#: Terminal states. A task in one of these states is included only when
#: ``delivered_at_turn`` is set AND the delivery is within the
#: ``recent_turns_for_done_inclusion`` window.
_TERMINAL_STATES = frozenset({"done", "failed", "superseded"})


@dataclass(frozen=True)
class StateBlockEntry:
    """One STATE block row after cap + eviction.

    The provider emits a single :class:`ContextEntry` whose
    ``payload["content"]`` is the rendered text of every row joined by
    newlines. This dataclass is the intermediate representation used
    for tests + recency classification.
    """

    task_id: str
    title: str
    state: str
    last_update_1liner: str
    delivered_at_turn: int | None
    last_event_id: str | None
    lineage: list[str]
    age_min: float
    age_turns: int
    recency: str  # "active" | "stale"


class StateBlockProvider:
    """Compose the STATE block for the bounded Jarvis prompt.

    Construction args:

    - ``task_store`` — source of truth for the candidate task rows.
    - ``state_policy`` — caps + truncation knobs. Defaults to
      :func:`default_state_policy`.
    - ``recency_policy`` — active / stale thresholds. Defaults to
      :func:`default_recency_policy`.
    - ``eviction_strategy`` — pluggable eviction order. Defaults to
      :class:`DefaultEvictionStrategy`.
    - ``current_user_turn`` — monotonic user-turn index used to compute
      ``age_turns`` and to gate the post-delivery inclusion window.
    - ``last_referenced_turn_by_task`` — mapping of ``task_id`` → most
      recent user-turn index that touched the task. Defaults to ``{}``;
      the orchestrator wires this from the recent-turns history.
    - ``now`` — clock callable returning a :class:`datetime`. Defaults
      to :meth:`datetime.utcnow`. Injectable so the snapshot tests can
      pin ``age_min`` to a stable value.
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        state_policy: StatePolicy | None = None,
        recency_policy: RecencyPolicy | None = None,
        eviction_strategy: EvictionStrategy | None = None,
        current_user_turn: int = 0,
        last_referenced_turn_by_task: dict[str, int] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._task_store = task_store
        self._state_policy = state_policy or default_state_policy()
        self._recency_policy = recency_policy or default_recency_policy()
        self._eviction = eviction_strategy or DefaultEvictionStrategy()
        self._current_user_turn = current_user_turn
        self._referenced = dict(last_referenced_turn_by_task or {})
        self._now = now or (lambda: datetime.now(UTC).replace(tzinfo=None))

    @property
    def provider_id(self) -> str:
        return STATE_BLOCK_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        candidates = self._candidate_tasks()
        if not candidates:
            return []

        rows = [self._build_entry(task) for task in candidates]
        rows = self._apply_cap(rows)
        if not rows:
            return []

        rendered = _render_state_block(rows)
        return [
            ContextEntry(
                id=f"{STATE_BLOCK_PROVIDER_ID}:turn-{self._current_user_turn}",
                kind="system_note",
                source="state_block_provider",
                token_estimate=len(rendered) // 4,
                pinned=True,
                created_at="",
                provider_id=STATE_BLOCK_PROVIDER_ID,
                payload={"role": "system", "content": rendered},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        ]

    # --- Internals -----------------------------------------------------------

    def _candidate_tasks(self) -> list[Task]:
        """Return the tasks that should be considered for the STATE block.

        Active set = live (running / spawned / awaiting_input) UNION
        terminal-and-delivered-within-window. ``dismissed`` rows are
        excluded by :meth:`TaskStore.list_tasks` default.
        """

        try:
            tasks = self._task_store.list_tasks()
        except TaskStoreError:
            return []

        result: list[Task] = []
        window = self._state_policy.recent_turns_for_done_inclusion
        for task in tasks:
            if task.state in _LIVE_STATES:
                result.append(task)
                continue
            if task.state in _TERMINAL_STATES:
                if task.delivered_at_turn is None:
                    # A freshly-completed task that has not been
                    # announced yet — surface it so Jarvis can deliver
                    # the result on the next turn.
                    result.append(task)
                    continue
                gap = self._current_user_turn - task.delivered_at_turn
                if gap <= window:
                    result.append(task)
        return result

    def _build_entry(self, task: Task) -> StateBlockEntry:
        title = _shorten_title(task.title, max_words=self._state_policy.title_max_words)
        update = _shorten_update(
            task.result or task.goal,
            max_chars=self._state_policy.update_max_chars,
        )
        age_min, age_seconds = _age_at_assembly(task.updated_at, now=self._now())
        last_ref = self._referenced.get(task.id, -1)
        if last_ref < 0:
            age_turns = max(0, self._current_user_turn)
        else:
            age_turns = max(0, self._current_user_turn - last_ref)
        decision = classify_recency(
            RecencySignal(age_turns=age_turns, age_seconds=age_seconds),
            policy=self._recency_policy,
        )
        return StateBlockEntry(
            task_id=task.id,
            title=title,
            state=task.state,
            last_update_1liner=update,
            delivered_at_turn=task.delivered_at_turn,
            last_event_id=None,  # 0052 wires last_event_id provenance.
            lineage=list(task.lineage),
            age_min=age_min,
            age_turns=age_turns,
            recency=decision,
        )

    def _apply_cap(self, rows: list[StateBlockEntry]) -> list[StateBlockEntry]:
        if len(rows) <= self._state_policy.max_entries:
            return rows
        candidates = [
            StateBlockCandidate(
                task_id=r.task_id,
                state=r.state,
                delivered_at_turn=r.delivered_at_turn,
                order_key=(_order_marker(r), idx),
            )
            for idx, r in enumerate(rows)
        ]
        survivors = self._eviction.evict_to_cap(
            candidates,
            cap=self._state_policy.max_entries,
        )
        survivor_ids = {c.task_id for c in survivors}
        return [r for r in rows if r.task_id in survivor_ids]


def _order_marker(row: StateBlockEntry) -> int:
    """Combine ``delivered_at_turn`` with ``age_turns`` for stable sort keys.

    Older delivered rows sort to the front so the eviction strategy
    drops them first. Live rows fall back to ``age_turns`` so the
    oldest-referenced one is closest to the front (still kept by the
    ``never-evict-running`` rule for ``running`` entries).
    """

    if row.delivered_at_turn is not None:
        return row.delivered_at_turn
    return -row.age_turns


def _shorten_title(title: str, *, max_words: int) -> str:
    words = title.split()
    if len(words) <= max_words:
        return title.strip()
    return " ".join(words[:max_words]) + "…"


def _shorten_update(text: str, *, max_chars: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    if max_chars <= 1:
        return flat[:max_chars]
    return flat[: max_chars - 1] + "…"


def _age_at_assembly(updated_at: str, *, now: datetime) -> tuple[float, float]:
    """Return ``(age_min, age_seconds)`` always recomputed at call time.

    PRD-loud: never persisted. We tolerate timestamps stored without
    a timezone marker (SQLite's ``datetime('now')`` produces
    ``YYYY-MM-DD HH:MM:SS`` UTC by convention).
    """

    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(updated_at)
    except ValueError:
        parsed = None
    if parsed is None:
        return 0.0, 0.0
    delta = (now - parsed).total_seconds()
    if delta < 0:
        delta = 0.0
    return delta / 60.0, delta


def _render_state_block(rows: Sequence[StateBlockEntry]) -> str:
    """Render the STATE block as a single multi-line system message.

    Lines are deterministic — same inputs → same string — so the
    golden snapshot tests can pin the layout. Each row is a single
    line with structured fields so the LLM can pick the relevant id
    + recency signal without scanning prose.
    """

    lines = [
        "STATE (tâches actives, PRD 0006). Pour chaque tâche : "
        "id, titre, état, dernière mise à jour, recency, delivered_at_turn, lineage."
    ]
    for row in rows:
        lineage_repr = json.dumps(row.lineage, ensure_ascii=False)
        lines.append(
            f"- id={row.task_id} "
            f'title="{row.title}" '
            f"state={row.state} "
            f'last_update_1liner="{row.last_update_1liner}" '
            f"age_min={row.age_min:.1f} "
            f"recency={row.recency} "
            f"delivered_at_turn={row.delivered_at_turn} "
            f"lineage={lineage_repr}"
        )
    lines.append(
        "Utilise ``recency=active`` pour répondre par « Voilà X » ; "
        "``recency=stale`` pour « Tu m'avais demandé X, voilà… »."
    )
    return "\n".join(lines)


__all__ = [
    "STATE_BLOCK_PROVIDER_ID",
    "StateBlockEntry",
    "StateBlockProvider",
]


def _coerce_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Compatibility helper for future provider hooks; currently unused."""

    return payload or {}
