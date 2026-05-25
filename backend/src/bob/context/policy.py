"""Configuration object passed to :class:`bob.context.assembler.ContextAssembler`.

Issue 0043 only needs a way to identify the active provider set; later
slices wire in token budgets, recency windows, eviction strategy id and
state-block caps. Keeping :class:`ContextPolicy` minimal here means the
data structure for the foundation is stable from day one — future fields
are added as defaulted Optionals so existing call sites stay valid.

The PRD pins (see ``## Implementation Decisions / Modules / context/``) the
following fields for the full v2 surface; we mirror them here as ``None``
defaults so type-checkers do not complain when callers omit them:

* ``token_budget`` — overall prompt token cap (used in 0046+).
* ``recent_turns_window`` — K user/assistant pairs visible verbatim
  (``RecentTurnsProvider``).
* ``state_cap`` — max STATE-block entries (``StateBlockProvider``).
* ``eviction_policy_id`` — id of the :class:`bob.context.eviction.EvictionStrategy`
  to apply (``"recency"`` by default, ``"least_referenced"`` etc.).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

#: Default policy id used by the orchestrator in issue 0043. The matching
#: provider list is :class:`bob.context.providers.legacy_full_history.LegacyFullHistoryProvider`.
LEGACY_FULL_HISTORY_POLICY_ID = "legacy_full_history"


@dataclass(frozen=True)
class ContextPolicy:
    """How :class:`ContextAssembler` should compose providers into a prompt.

    Fields:

    - ``policy_id`` — short string used in logs/tests to identify which
      policy was active for a turn. ``"legacy_full_history"`` is the
      default that reproduces today's behavior.
    - ``provider_ids`` — ordered list of provider identifiers that the
      :class:`ContextAssembler` will look up in its provider registry. The
      assembler emits entries in this exact order; entries are never
      reordered within a provider.
    - ``token_budget`` / ``recent_turns_window`` / ``state_cap`` /
      ``eviction_policy_id`` — placeholders for later slices. They have
      sensible defaults so issue 0043 code does not need to thread real
      values, but the field is reserved so tests can assert the dataclass
      shape upfront.
    """

    policy_id: str = LEGACY_FULL_HISTORY_POLICY_ID
    provider_ids: Sequence[str] = field(default_factory=lambda: ("legacy_full_history",))
    token_budget: int | None = None
    recent_turns_window: int | None = None
    state_cap: int | None = None
    eviction_policy_id: str | None = None


def legacy_full_history_policy() -> ContextPolicy:
    """Convenience constructor for the default policy used by orchestrator v1.

    Equivalent to :class:`ContextPolicy()` but explicit at call sites so the
    intent ("reproduce the pre-0043 behavior") is obvious in code review.
    """

    return ContextPolicy(
        policy_id=LEGACY_FULL_HISTORY_POLICY_ID,
        provider_ids=("legacy_full_history",),
    )


def parse_policy_overrides(
    *,
    policy_id: str | None = None,
    provider_ids: Sequence[str] | None = None,
    token_budget: int | None = None,
    recent_turns_window: int | None = None,
    state_cap: int | None = None,
    eviction_policy_id: str | None = None,
) -> ContextPolicy:
    """Build a :class:`ContextPolicy` from optional overrides on top of defaults.

    Any field left as ``None`` falls back to the default from
    :func:`legacy_full_history_policy`. This is the canonical entry point
    for tests and future config-driven wiring — call sites pass only what
    they want to change.
    """

    default = legacy_full_history_policy()
    return ContextPolicy(
        policy_id=policy_id if policy_id is not None else default.policy_id,
        provider_ids=tuple(provider_ids) if provider_ids is not None else default.provider_ids,
        token_budget=token_budget if token_budget is not None else default.token_budget,
        recent_turns_window=(
            recent_turns_window if recent_turns_window is not None else default.recent_turns_window
        ),
        state_cap=state_cap if state_cap is not None else default.state_cap,
        eviction_policy_id=(
            eviction_policy_id if eviction_policy_id is not None else default.eviction_policy_id
        ),
    )
