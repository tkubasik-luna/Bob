"""Configuration object passed to :class:`bob.context.assembler.ContextAssembler`.

Issue 0043 introduced the foundation with the legacy "send the whole
thread every turn" default. Issue 0046 adds the bounded providers
(:class:`SystemBlockProvider`, :class:`RollingSummaryProvider`,
:class:`RecentTurnsProvider`, :class:`UserMessageProvider`) and exposes
:func:`bounded_v1_policy` as the new default. The legacy policy stays
available for regression tests + the byte-for-byte snapshot.

Recap of the v2 surface (PRD `## Implementation Decisions / Modules /
context/`):

* ``token_budget`` — overall prompt token cap (enforced by 0046+ tests).
* ``recent_turns_window`` — K user/assistant pairs visible verbatim
  (:class:`RecentTurnsProvider`).
* ``state_cap`` — max STATE-block entries (:class:`StateBlockProvider`,
  shipped in 0050).
* ``eviction_policy_id`` — id of the :class:`bob.context.eviction.EvictionStrategy`
  to apply (``"recency"`` by default).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

#: Legacy "send the whole thread every turn" policy id. Issue 0043 default;
#: retained in 0046 as a regression / safety-net policy.
LEGACY_FULL_HISTORY_POLICY_ID = "legacy_full_history"

#: Bounded policy id introduced by issue 0046. Kept as a regression
#: target; the orchestrator default is now :func:`bounded_v2_policy`.
BOUNDED_V1_POLICY_ID = "bounded_v1"

#: Bounded policy id introduced by issue 0051. Same provider mix as v1
#: plus the cross-epoch digest slotted right after the system block.
#: PRD 0006's "Sealed epochs are NEVER auto-injected — only the cross-
#: epoch digest is" lives in this policy.
BOUNDED_V2_POLICY_ID = "bounded_v2"

#: Default recent-turns window (K) for the bounded policy. K user↔Jarvis
#: pairs are visible verbatim — i.e. up to 2*K rows in the persisted
#: history.
DEFAULT_RECENT_TURNS_WINDOW = 3

#: Default overall prompt token budget for the bounded policy. Picked so
#: the long-session smoke test plateaus around turn 30 with K=3.
DEFAULT_TOKEN_BUDGET = 2048


@dataclass(frozen=True)
class ContextPolicy:
    """How :class:`ContextAssembler` should compose providers into a prompt.

    Fields:

    - ``policy_id`` — short string used in logs/tests to identify which
      policy was active for a turn.
    - ``provider_ids`` — ordered list of provider identifiers that the
      :class:`ContextAssembler` will look up in its provider registry. The
      assembler emits entries in this exact order; entries are never
      reordered within a provider.
    - ``token_budget`` / ``recent_turns_window`` / ``state_cap`` /
      ``eviction_policy_id`` — see the module docstring.
    """

    policy_id: str = LEGACY_FULL_HISTORY_POLICY_ID
    provider_ids: Sequence[str] = field(default_factory=lambda: ("legacy_full_history",))
    token_budget: int | None = None
    recent_turns_window: int | None = None
    state_cap: int | None = None
    eviction_policy_id: str | None = None


def legacy_full_history_policy() -> ContextPolicy:
    """Convenience constructor for the legacy "whole thread" policy.

    Retained as a regression target; production orchestrator wires
    :func:`bounded_v1_policy`.
    """

    return ContextPolicy(
        policy_id=LEGACY_FULL_HISTORY_POLICY_ID,
        provider_ids=("legacy_full_history",),
    )


def bounded_v1_policy() -> ContextPolicy:
    """Return the bounded policy: system + rolling summary + recent turns + live user.

    The provider order is significant — entries are emitted in this exact
    order:

    1. ``system_block`` — personality + tool-schema reminder + (optional)
       waiting-input addendum.
    2. ``rolling_summary`` — system-role block carrying the latest
       persisted summary of older turns (skipped when the store is empty).
    3. ``recent_turns`` — verbatim window of the last K user↔Jarvis pairs.
    4. ``user_message`` — the live in-progress user turn passed via
       :class:`AssemblyContext`.

    PRD 0006 STATE block (issue 0050) will slot in between
    ``system_block`` and ``rolling_summary``; this slice operates without
    it.
    """

    return ContextPolicy(
        policy_id=BOUNDED_V1_POLICY_ID,
        provider_ids=("system_block", "rolling_summary", "recent_turns", "user_message"),
        token_budget=DEFAULT_TOKEN_BUDGET,
        recent_turns_window=DEFAULT_RECENT_TURNS_WINDOW,
    )


def bounded_v2_policy() -> ContextPolicy:
    """Return the bounded v2 policy — adds the cross-epoch digest block.

    Provider order:

    1. ``system_block`` — personality + tool-schema reminder + (optional)
       waiting-input addendum.
    2. ``cross_epoch_digest`` — synthesis of sealed epochs, regenerated
       from RAW sealed turns at every seal (issue 0051). Skipped when no
       epoch has sealed yet.
    3. ``rolling_summary`` — current-epoch rolling summary of older
       turns (skipped when empty).
    4. ``recent_turns`` — verbatim window of the last K user↔Jarvis pairs.
    5. ``user_message`` — the live in-progress user turn.

    PRD 0006 STATE block (issue 0050) will slot in between
    ``system_block`` and ``cross_epoch_digest``. The ordering is
    deliberate: STATE > sealed epochs > current epoch > live user.
    """

    return ContextPolicy(
        policy_id=BOUNDED_V2_POLICY_ID,
        provider_ids=(
            "system_block",
            "cross_epoch_digest",
            "rolling_summary",
            "recent_turns",
            "user_message",
        ),
        token_budget=DEFAULT_TOKEN_BUDGET,
        recent_turns_window=DEFAULT_RECENT_TURNS_WINDOW,
    )


def parse_policy_overrides(
    *,
    policy_id: str | None = None,
    provider_ids: Sequence[str] | None = None,
    token_budget: int | None = None,
    recent_turns_window: int | None = None,
    state_cap: int | None = None,
    eviction_policy_id: str | None = None,
    base: ContextPolicy | None = None,
) -> ContextPolicy:
    """Build a :class:`ContextPolicy` from optional overrides on top of a base.

    Any field left as ``None`` falls back to the corresponding field on
    ``base`` (defaults to :func:`legacy_full_history_policy` for backwards
    compatibility with 0043 call sites). This is the canonical entry point
    for tests and future config-driven wiring — call sites pass only what
    they want to change.
    """

    default = base if base is not None else legacy_full_history_policy()
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
