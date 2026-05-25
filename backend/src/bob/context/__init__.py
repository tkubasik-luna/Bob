"""Context-assembly module (Jarvis v2 — PRD 0006).

Public surface:

- :class:`ContextEntry` — versioned dataclass shared by every provider.
- :class:`ContextProvider` — protocol any concrete provider implements.
- :class:`AssemblyContext` — read-only inputs passed to each provider call.
- :class:`ContextPolicy` — provider list + budgets.
- :class:`ContextAssembler` — pure composition of providers into chat
  messages.

Concrete providers live under :mod:`bob.context.providers`. Issue 0046
extends the foundation with the bounded providers
(:class:`SystemBlockProvider`, :class:`RollingSummaryProvider`,
:class:`RecentTurnsProvider`, :class:`UserMessageProvider`) plus the
:class:`Summariser` module and prompt-fragment registry.
"""

from __future__ import annotations

from bob.context.assembler import ContextAssembler, ContextAssemblerError
from bob.context.entry import (
    CONTEXT_ENTRY_SCHEMA_VERSION,
    ContextEntry,
    ContextEntryKind,
)
from bob.context.eviction import (
    DefaultEvictionStrategy,
    EvictionStrategy,
    StateBlockCandidate,
)
from bob.context.policy import (
    BOUNDED_V1_POLICY_ID,
    BOUNDED_V2_POLICY_ID,
    DEFAULT_RECENT_TURNS_WINDOW,
    DEFAULT_TOKEN_BUDGET,
    LEGACY_FULL_HISTORY_POLICY_ID,
    ContextPolicy,
    bounded_v1_policy,
    bounded_v2_policy,
    legacy_full_history_policy,
    parse_policy_overrides,
)
from bob.context.provider import AssemblyContext, ContextProvider
from bob.context.recency import (
    RecencyDecision,
    RecencyPolicy,
    RecencySignal,
    classify_recency,
    default_recency_policy,
)
from bob.context.state_policy import StatePolicy, default_state_policy
from bob.context.tokenizer import Tokenizer, WordCountTokenizer, default_tokenizer

__all__ = [
    "BOUNDED_V1_POLICY_ID",
    "BOUNDED_V2_POLICY_ID",
    "CONTEXT_ENTRY_SCHEMA_VERSION",
    "DEFAULT_RECENT_TURNS_WINDOW",
    "DEFAULT_TOKEN_BUDGET",
    "LEGACY_FULL_HISTORY_POLICY_ID",
    "AssemblyContext",
    "ContextAssembler",
    "ContextAssemblerError",
    "ContextEntry",
    "ContextEntryKind",
    "ContextPolicy",
    "ContextProvider",
    "DefaultEvictionStrategy",
    "EvictionStrategy",
    "RecencyDecision",
    "RecencyPolicy",
    "RecencySignal",
    "StateBlockCandidate",
    "StatePolicy",
    "Tokenizer",
    "WordCountTokenizer",
    "bounded_v1_policy",
    "bounded_v2_policy",
    "classify_recency",
    "default_recency_policy",
    "default_state_policy",
    "default_tokenizer",
    "legacy_full_history_policy",
    "parse_policy_overrides",
]
