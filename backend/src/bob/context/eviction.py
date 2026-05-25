"""Injectable :class:`EvictionStrategy` (PRD 0006 / issue 0050).

When the STATE block has more candidates than the
:class:`bob.context.state_policy.StatePolicy.max_entries` cap, the
provider drops the lowest-priority entries until the count fits.
The *order* in which entries are evicted is a policy decision —
PRD-prescribed default is:

1. ``done`` / ``superseded`` tasks already delivered to the user
   (oldest delivered first).
2. ``failed`` tasks (oldest first).
3. ``awaiting_input`` tasks (oldest first; the user is the only one
   who can move them off this state, so prompt churn is acceptable).
4. **Never** ``running`` tasks.

The strategy is wrapped in a :class:`typing.Protocol` so future tuning
(e.g. preferring "task referenced by current user message") is a
constructor swap, not a code rewrite. The default implementation is
deterministic given identical inputs — the snapshot tests rely on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StateBlockCandidate:
    """Minimum shape :class:`EvictionStrategy` needs from a candidate row.

    Decoupled from :class:`bob.task_store.Task` so tests can build a
    candidate list inline without going through SQLite. The provider
    builds these from real ``Task`` rows.

    ``order_key`` is a tuple ``(epoch_marker, rowid)`` already computed
    by the provider so eviction stays a pure sort — no peeking at
    SQLite timestamps inside the strategy.
    """

    task_id: str
    state: str
    delivered_at_turn: int | None
    order_key: tuple[int, int]


class EvictionStrategy(Protocol):
    """Decide which STATE candidates to drop when the cap is exceeded.

    Implementations return a *new* list with at most ``cap`` entries.
    Stable ordering: the caller emits the result in the order the
    strategy returns. The PRD prescribes "running tasks are never
    evicted", so an implementation that drops a ``running`` entry to
    fit the cap is a behavioural violation — the
    :class:`DefaultEvictionStrategy` upholds the invariant via the
    ``_PRIORITY`` ordering below.
    """

    def evict_to_cap(
        self,
        candidates: Sequence[StateBlockCandidate],
        *,
        cap: int,
    ) -> list[StateBlockCandidate]: ...  # pragma: no cover — protocol member.


# Lower priority = evicted first. The integers are arbitrary as long as
# they preserve the documented order; the comparison below relies on
# the relative ranking only.
_PRIORITY: dict[str, int] = {
    # Delivered terminal rows (``done`` / ``superseded`` post-delivery
    # window) are the cheapest to drop — the user already heard the
    # result. We rely on ``delivered_at_turn`` being set before placing
    # the candidate in this bucket; the provider enforces that.
    "delivered_done": 0,
    "superseded": 1,
    "failed": 2,
    "awaiting_input": 3,
    "spawned": 4,
    "running": 100,  # never evicted in practice — cap dwarfs the running
    # set (3 < 8), but ``100`` keeps the strategy honest for adversarial
    # tests with tiny caps.
}


class DefaultEvictionStrategy:
    """Built-in :class:`EvictionStrategy` matching the PRD ordering.

    Algorithm:

    1. Annotate every candidate with its priority bucket. A ``done`` /
       ``superseded`` row with ``delivered_at_turn`` set falls into the
       ``delivered_done`` bucket (lowest priority). A ``done`` row
       *without* ``delivered_at_turn`` belongs to a queued completion
       — we surface it as ``spawned`` priority so Jarvis sees it on the
       next turn.
    2. Sort by ``(priority asc, order_key asc)``. Entries with the
       lowest priority + the oldest ``order_key`` end up at the front
       of the eviction queue.
    3. Drop entries from the front until ``len(survivors) <= cap``.
    4. Return survivors in their *original* order so the provider
       emits the STATE block in stable, chronologically-meaningful
       order.
    """

    def evict_to_cap(
        self,
        candidates: Sequence[StateBlockCandidate],
        *,
        cap: int,
    ) -> list[StateBlockCandidate]:
        if cap < 0:
            raise ValueError(f"cap must be >= 0, got {cap}")
        if len(candidates) <= cap:
            return list(candidates)

        annotated = [(_bucket_for(c), c) for c in candidates]
        # Sort the eviction order: lowest priority first, then oldest
        # ``order_key``. The original list order is preserved on
        # survivors by walking ``candidates`` again at the end.
        sorted_evictable = sorted(
            annotated,
            key=lambda pair: (_PRIORITY[pair[0]], pair[1].order_key),
        )
        # Mark drop set in priority order.
        drop_count = len(candidates) - cap
        dropped_ids: set[str] = set()
        for _bucket, candidate in sorted_evictable:
            if len(dropped_ids) >= drop_count:
                break
            if _bucket == "running":
                # Never evict ``running`` — propagate the invariant up
                # to the caller via an under-cap survivors set rather
                # than silently dropping a running row.
                continue
            dropped_ids.add(candidate.task_id)

        # Survivors keep their original ordering. If we could not drop
        # enough non-running entries to satisfy the cap, the survivors
        # list is longer than ``cap`` — the provider treats this as an
        # explicit overflow and may emit additional logging.
        return [c for c in candidates if c.task_id not in dropped_ids]


def _bucket_for(candidate: StateBlockCandidate) -> str:
    """Map a candidate to one of the priority buckets above.

    Folds the ``delivered_at_turn`` flag into the ``done`` /
    ``superseded`` decisions so the strategy stays a pure sort.
    """

    state = candidate.state
    if state in ("done", "superseded"):
        if candidate.delivered_at_turn is not None:
            return "delivered_done"
        # Undelivered ``done`` — should still be visible so Jarvis can
        # announce it. Place it in the ``spawned`` bucket (low pressure
        # but evictable behind delivered + failed).
        return "spawned"
    if state == "failed":
        return "failed"
    if state in ("awaiting_input", "waiting_input"):
        return "awaiting_input"
    if state in ("spawned", "pending"):
        return "spawned"
    if state == "running":
        return "running"
    # Unknown state — be conservative, treat as evictable-mid-priority.
    return "failed"
