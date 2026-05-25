"""StatePolicy — STATE-block tuning dial (PRD 0006 / issue 0050).

The STATE block lists active tasks at the top of every Jarvis prompt so
the LLM can pick the right ``task_id`` for ``addendum_task`` /
``cancel_task`` / ``replan_task`` and use recency-aware phrasing when
delivering results. :class:`StatePolicy` collects every cap the
:class:`bob.context.providers.state_block.StateBlockProvider` enforces:

* ``max_entries`` — hard cap on the number of STATE rows emitted in a
  single turn. The PRD pins this at ``8`` — enough for the typical
  burst (cap 3 running + cap 5 queued) without crowding the prompt.
* ``title_max_words`` — title shortener cap (``8`` words by default,
  matching the PRD wording). Titles longer than the cap are truncated
  with a trailing ellipsis.
* ``update_max_chars`` — char cap on ``last_update_1liner`` (``120``).
  Mirrors the PRD limit; everything past that is truncated with an
  ellipsis.
* ``recent_turns_for_done_inclusion`` — how many user turns a
  ``done``/``failed``/``superseded`` task lingers in the STATE block
  AFTER ``delivered_at_turn`` has been set. The PRD says "done tasks
  within last K user turns", default ``K=3`` to mirror the bounded
  recent-turns window so the user can still address a result with
  natural ``"refais le"`` references for a few turns post-delivery.

Token budget is asserted in the golden snapshot test, not enforced at
runtime — keeping the policy small and the enforcement purely declared
on entry-count avoids per-call tokeniser plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatePolicy:
    """Hard caps applied by :class:`StateBlockProvider` per turn.

    See the module docstring for field semantics. The dataclass is
    frozen so a policy is safe to share across providers / tests.
    """

    max_entries: int = 8
    title_max_words: int = 8
    update_max_chars: int = 120
    recent_turns_for_done_inclusion: int = 3

    def __post_init__(self) -> None:
        if self.max_entries < 1:
            raise ValueError(f"StatePolicy.max_entries must be >= 1, got {self.max_entries}")
        if self.title_max_words < 1:
            raise ValueError(
                f"StatePolicy.title_max_words must be >= 1, got {self.title_max_words}"
            )
        if self.update_max_chars < 1:
            raise ValueError(
                f"StatePolicy.update_max_chars must be >= 1, got {self.update_max_chars}"
            )
        if self.recent_turns_for_done_inclusion < 0:
            raise ValueError(
                "StatePolicy.recent_turns_for_done_inclusion must be >= 0, "
                f"got {self.recent_turns_for_done_inclusion}"
            )


def default_state_policy() -> StatePolicy:
    """Return the production :class:`StatePolicy` (PRD-prescribed defaults)."""

    return StatePolicy()
