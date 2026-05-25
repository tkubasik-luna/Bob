"""Per-tool :class:`RetryPolicy` table (PRD 0006 / issue 0048).

Centralises the retry budget + degrade action + ``accept_partial`` flag
for every Jarvis-side tool. The retry counter itself rides on the
in-memory :class:`bob.validation.envelope.CallEnvelope` (never persisted
to a :class:`bob.context.ContextEntry`) so policy changes only need to
touch this module.

Design choices:

- A tool with no explicit entry falls back to :data:`DEFAULT_POLICY`.
  This keeps new tools safe by default and lets later slices register
  their own row without touching every call site.
- ``degrade_action`` is intentionally an enum-ish literal so call sites
  can branch on it without parsing a string ad-hoc. Today we only ship
  ``"hardcoded_say"`` (Jarvis) and ``"forced_done_failed"`` (sub-agent);
  more actions are easy to add as the surface grows.
- ``accept_partial`` opts the tool into a one-pass "drop unknown keys
  + validate required-only" mode. Saves a retry on the common
  "valid required + garbage optional" case while still rejecting when
  a required field is missing or malformed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Closed set of degrade actions Jarvis / sub-agent dispatchers know how
#: to perform after exhausting :attr:`RetryPolicy.max_retries`.
DegradeAction = Literal["hardcoded_say", "forced_done_failed"]


@dataclass(frozen=True)
class RetryPolicy:
    """Per-tool retry knobs.

    Fields:

    - ``max_retries`` — number of *re-attempts* after the first failure.
      ``0`` means "one try, no retries". The transient
      :class:`bob.validation.envelope.CallEnvelope` carries the live
      counter; this module only describes the cap.
    - ``degrade_action`` — what the dispatcher does when the budget is
      exhausted. See :data:`DegradeAction`.
    - ``accept_partial`` — when ``True``, the dispatcher first strips
      unknown keys from the LLM-provided arguments and validates against
      the required-only subset of the model. A retry is only triggered
      when a required field is missing or invalid. Defaults to ``False``
      to preserve strict validation for tools that need every field
      validated.
    """

    max_retries: int = 1
    degrade_action: DegradeAction = "hardcoded_say"
    accept_partial: bool = False


#: Default policy used when a tool has no explicit row in :data:`POLICY_TABLE`.
#: The Jarvis-side default mirrors the historical
#: :mod:`bob.response_parser` behaviour: one retry, then degrade to the
#: hardcoded "Désolé, peux-tu reformuler ?" phrase.
DEFAULT_POLICY = RetryPolicy(
    max_retries=1,
    degrade_action="hardcoded_say",
    accept_partial=False,
)


#: Sub-agent default. When the sub-agent's *runner* exhausts its retry
#: budget it forces a ``done(failed, invalid_output)`` instead of a
#: hardcoded say (the user never sees sub-agent output directly).
SUB_AGENT_DEFAULT_POLICY = RetryPolicy(
    max_retries=1,
    degrade_action="forced_done_failed",
    accept_partial=False,
)


#: Per-tool overrides. Tools not listed here fall back to
#: :data:`DEFAULT_POLICY`. We accept partial args on the unified ``say``
#: tool because the LLM frequently emits a valid ``speech`` plus extra
#: garbage keys ("emotion", "tone"...) under temperature; dropping the
#: unknown keys lets the turn succeed first try.
POLICY_TABLE: dict[str, RetryPolicy] = {
    "say": RetryPolicy(
        max_retries=1,
        degrade_action="hardcoded_say",
        accept_partial=True,
    ),
    "spawn_subtask": RetryPolicy(
        max_retries=1,
        degrade_action="hardcoded_say",
        accept_partial=False,
    ),
    "forward_to_subtask": RetryPolicy(
        max_retries=1,
        degrade_action="hardcoded_say",
        accept_partial=False,
    ),
    "cancel_subtask": RetryPolicy(
        max_retries=1,
        degrade_action="hardcoded_say",
        accept_partial=False,
    ),
}


def get_policy(tool_name: str) -> RetryPolicy:
    """Look up the policy for ``tool_name``, falling back to the default."""

    return POLICY_TABLE.get(tool_name, DEFAULT_POLICY)


__all__ = [
    "DEFAULT_POLICY",
    "POLICY_TABLE",
    "SUB_AGENT_DEFAULT_POLICY",
    "DegradeAction",
    "RetryPolicy",
    "get_policy",
]
