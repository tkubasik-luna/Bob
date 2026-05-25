"""RecencyPolicy — active vs stale phrasing signal (PRD 0006 / issue 0050).

The PRD instructs Jarvis to phrase task-result deliveries differently
depending on whether the user is still on topic. "Voilà X" feels right
when the user just asked; "Tu m'avais demandé X, voilà…" feels right
when many turns have elapsed.

Pre-0050 this decision lived in inline string templates inside the
dispatcher (slice #0021 ``ASK_USER_PARAPHRASE_TEMPLATE``, #0025
``DONE_SYNTHESIS_TEMPLATE``). The PRD lifts the decision into a pure
:class:`RecencyPolicy` struct computed at turn-assembly time so:

1. The Jarvis prompt receives a structured ``recency`` signal and the
   LLM picks the phrasing — no hardcoded French strings in dispatch
   code.
2. The decision is deterministic: identical inputs → identical
   classification. Snapshot tests pin the classifier outcome on a few
   representative fixtures.
3. Future signals (topic overlap, last_event_id age) can be wired in
   by extending the struct, not by refactoring call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Closed set of classifications the policy may produce. The Jarvis
#: prompt template references these literals verbatim so a typo would
#: surface loudly in golden tests.
RecencyDecision = Literal["active", "stale"]


@dataclass(frozen=True)
class RecencyPolicy:
    """Thresholds used by :func:`classify_recency` (PRD 0006 / issue 0050).

    Fields:

    - ``active_within_user_turns`` — a task is ``active`` if the gap
      between the current user-turn index and the most recent reference
      to the task is at most this many user turns. Defaults to ``3``,
      matching the bounded ``recent_turns_window`` so a freshly-asked
      sub-task stays active for the duration of the verbatim window.
    - ``active_within_seconds`` — additionally a task is ``active`` if
      the age (now - last_update_at) is below this many seconds.
      Defaults to ``120 s`` so a long burst of typing on an unrelated
      topic does not flip the result phrasing while the original ask
      is still warm on screen.
    - ``topic_overlap_min`` — token-overlap threshold between the
      task goal/title and the current user message. ``0`` disables
      the topic-overlap signal; the slice ships the dial without
      wiring a tokeniser to keep the policy footprint small. Future
      revisions can crank this without a new module.

    A task is ``active`` if any of the three signals fires. Otherwise
    it is ``stale``.
    """

    active_within_user_turns: int = 3
    active_within_seconds: float = 120.0
    topic_overlap_min: float = 0.0

    def __post_init__(self) -> None:
        if self.active_within_user_turns < 0:
            raise ValueError(
                "RecencyPolicy.active_within_user_turns must be >= 0, "
                f"got {self.active_within_user_turns}"
            )
        if self.active_within_seconds < 0:
            raise ValueError(
                "RecencyPolicy.active_within_seconds must be >= 0, "
                f"got {self.active_within_seconds}"
            )
        if not 0.0 <= self.topic_overlap_min <= 1.0:
            raise ValueError(
                f"RecencyPolicy.topic_overlap_min must be in [0, 1], got {self.topic_overlap_min}"
            )


@dataclass(frozen=True)
class RecencySignal:
    """Inputs handed to :func:`classify_recency` per candidate task.

    All fields are computed by the STATE-block provider at assembly
    time. ``age_turns`` and ``age_seconds`` are the gap between the
    current user-turn index / clock and the task's most recent
    reference. ``topic_overlap`` is normalised in ``[0, 1]`` (Jaccard
    over content tokens for the v1 wiring; ``0.0`` when unset).
    """

    age_turns: int
    age_seconds: float
    topic_overlap: float = 0.0


def classify_recency(
    signal: RecencySignal,
    *,
    policy: RecencyPolicy,
) -> RecencyDecision:
    """Return ``"active"`` when any signal fires below the policy threshold.

    Deterministic: identical inputs → identical decision. The PRD
    requires this so the snapshot tests can pin the classifier.

    Order of evaluation is documented but not load-bearing — every
    branch is independent and short-circuits via ``or``::

        active if (age_turns <= K_turns)
               or (age_seconds <= K_seconds)
               or (topic_overlap >= topic_min and topic_min > 0)
        stale otherwise.

    A topic-overlap threshold of ``0.0`` disables that signal so the
    policy ships sane defaults without forcing a tokeniser pass when
    callers do not need it.
    """

    if signal.age_turns <= policy.active_within_user_turns:
        return "active"
    if signal.age_seconds <= policy.active_within_seconds:
        return "active"
    if policy.topic_overlap_min > 0.0 and signal.topic_overlap >= policy.topic_overlap_min:
        return "active"
    return "stale"


def default_recency_policy() -> RecencyPolicy:
    """Return the production :class:`RecencyPolicy` (PRD-prescribed defaults)."""

    return RecencyPolicy()
