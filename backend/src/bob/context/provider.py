"""ContextProvider protocol + :class:`AssemblyContext` shared between providers.

A :class:`ContextProvider` is the unit of composition fed to
:class:`bob.context.assembler.ContextAssembler`. Each provider produces a
sequence of :class:`bob.context.entry.ContextEntry` objects given a small,
read-only :class:`AssemblyContext` (the live turn's user message + a snapshot
of the current :class:`bob.context.policy.ContextPolicy`).

The :class:`AssemblyContext` is intentionally tiny in issue 0043 — providers
only need it to plumb the current turn's user message through to the
``LegacyFullHistoryProvider`` so the assembled prompt matches today's
behavior verbatim. Later slices (0046 / 0050 / 0051) widen this with the
``epoch_id``, ``turn_index``, ``active_task_ids`` etc.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover — typing-only.
    from bob.context.entry import ContextEntry
    from bob.context.policy import ContextPolicy


@dataclass(frozen=True)
class AssemblyContext:
    """Read-only inputs visible to every provider during prompt assembly.

    Issue 0043 keeps this minimal: providers need the current user message
    (so the legacy provider can decide whether to fold it into history) and
    a reference to the policy so they can enforce their own budgets. Later
    slices add: ``turn_index``, ``epoch_id``, ``active_task_ids``,
    ``referenced_task_ids``, etc.
    """

    policy: ContextPolicy
    user_message: str | None = None


class ContextProvider(Protocol):
    """A pure source of :class:`ContextEntry` objects for one assembly.

    Implementations MUST NOT mutate any external state inside :meth:`entries`
    (no I/O, no time, no random). Reading from a store that is itself
    immutable for the duration of the call is fine — that is how
    :class:`bob.context.providers.legacy_full_history.LegacyFullHistoryProvider`
    handles the SQLite-backed thread.

    ``provider_id`` is a stable string that ends up on every
    :class:`ContextEntry` emitted by the provider; the assembler uses it for
    audit / debug, never for ordering.
    """

    @property
    def provider_id(self) -> str:  # pragma: no cover — protocol member.
        ...

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:  # pragma: no cover
        ...
