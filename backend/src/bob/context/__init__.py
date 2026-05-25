"""Context-assembly module (Jarvis v2 foundation, issue 0043).

Public surface:

- :class:`ContextEntry` — versioned dataclass shared by every provider.
- :class:`ContextProvider` — protocol any concrete provider implements.
- :class:`AssemblyContext` — read-only inputs passed to each provider call.
- :class:`ContextPolicy` — provider list + budgets.
- :class:`ContextAssembler` — pure composition of providers into chat
  messages.

Concrete providers live under :mod:`bob.context.providers`. Issue 0043 only
ships :class:`bob.context.providers.legacy_full_history.LegacyFullHistoryProvider`.
"""

from __future__ import annotations

from bob.context.assembler import ContextAssembler, ContextAssemblerError
from bob.context.entry import (
    CONTEXT_ENTRY_SCHEMA_VERSION,
    ContextEntry,
    ContextEntryKind,
)
from bob.context.policy import (
    LEGACY_FULL_HISTORY_POLICY_ID,
    ContextPolicy,
    legacy_full_history_policy,
    parse_policy_overrides,
)
from bob.context.provider import AssemblyContext, ContextProvider

__all__ = [
    "CONTEXT_ENTRY_SCHEMA_VERSION",
    "LEGACY_FULL_HISTORY_POLICY_ID",
    "AssemblyContext",
    "ContextAssembler",
    "ContextAssemblerError",
    "ContextEntry",
    "ContextEntryKind",
    "ContextPolicy",
    "ContextProvider",
    "legacy_full_history_policy",
    "parse_policy_overrides",
]
