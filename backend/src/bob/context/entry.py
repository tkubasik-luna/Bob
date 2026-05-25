"""Versioned :class:`ContextEntry` dataclass — the building block of Jarvis v2.

A context entry is a single piece of information that may end up inside the
prompt assembled for the LLM. Every entry carries an explicit ``kind`` (so
downstream providers can filter on type), a ``provider_id`` (so the
:class:`ContextAssembler` can audit which provider emitted it), a
``payload`` dict that carries kind-specific fields, and a ``schema_version``
that lets later slices evolve the shape without ambiguity.

Issue 0043 introduces the foundation with the full field set fixed upfront,
even though only :class:`bob.context.providers.legacy_full_history.LegacyFullHistoryProvider`
emits entries today. Later slices add bounded providers (``SystemBlockProvider``,
``StateBlockProvider``, ``RecentTurnsProvider``…) and the
:class:`bob.context.assembler.ContextAssembler` will compose them through
the same :class:`ContextEntry` type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Current schema version for :class:`ContextEntry`. Bumped when the field
#: set changes in a backwards-incompatible way. The migration in 0043
#: stamps existing rows with ``1``.
CONTEXT_ENTRY_SCHEMA_VERSION = 1


#: Closed set of kinds at issue 0043. Later slices extend this; keep the
#: literal narrow on purpose so a typo at a producer site fails type-check.
ContextEntryKind = Literal[
    "user_turn",
    "assistant_turn",
    "task_completed",
    "system_note",
]


@dataclass(frozen=True)
class ContextEntry:
    """A single piece of context that may end up inside the LLM prompt.

    Fields:

    - ``id`` — stable identifier (string). For migrated ``jarvis_messages``
      rows this is ``"jarvis_messages:{rowid}"``; new providers may use any
      shape (e.g. ``"task:{task_id}"``, UUID hex…).
    - ``kind`` — see :data:`ContextEntryKind`.
    - ``source`` — free-text origin label (``"jarvis_store"``, ``"task_store"``,
      ``"orchestrator"``, …). Used for debug / introspection, not for
      composition decisions.
    - ``token_estimate`` — rough token count for the entry's payload. Issue
      0043 only requires the field to exist; later slices use it to enforce
      :class:`bob.context.policy.ContextPolicy` budgets.
    - ``pinned`` — when ``True`` the entry is exempt from eviction in later
      providers. Defaults to ``False``.
    - ``created_at`` — ISO-8601 timestamp. Strings (not :class:`datetime`)
      so the type round-trips through SQLite text columns unchanged.
    - ``provider_id`` — id of the :class:`bob.context.provider.ContextProvider`
      that emitted the entry (``"legacy_full_history"`` for the v1 provider).
    - ``payload`` — kind-specific fields. For ``user_turn`` / ``assistant_turn``
      the payload carries at least ``{"role": "user"|"assistant", "content":
      "..."}``. Provider documentation pins the exact shape per kind.
    - ``schema_version`` — :data:`CONTEXT_ENTRY_SCHEMA_VERSION` at creation
      time.
    """

    id: str
    kind: ContextEntryKind
    source: str
    token_estimate: int
    pinned: bool
    created_at: str
    provider_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CONTEXT_ENTRY_SCHEMA_VERSION
