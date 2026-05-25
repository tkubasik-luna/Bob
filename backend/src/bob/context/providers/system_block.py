"""SystemBlockProvider — emits the system prompt entry for the bounded policy.

PRD 0006 / issue 0046. Under the bounded policy the system prompt is the
first entry of the assembled prompt. The provider receives the resolved
system content at construction time (the orchestrator composes it with
the personality + tool-schema reminder + waiting-input addendum) and
returns a single :class:`ContextEntry` carrying that content with
``role=system``.

Splitting the system block out of :class:`LegacyFullHistoryProvider` makes
the bounded prompt composition trivial: ``[system_block, rolling_summary,
recent_turns, user_message]``. The provider is stateless and pure.
"""

from __future__ import annotations

from collections.abc import Sequence

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry
from bob.context.provider import AssemblyContext

#: Stable id for this provider. Used as the ``provider_id`` on emitted
#: entries and as a key in the assembler's provider registry.
SYSTEM_BLOCK_PROVIDER_ID = "system_block"


class SystemBlockProvider:
    """Emit a single ``role=system`` :class:`ContextEntry`.

    Construction args:

    - ``system_content`` — the resolved system prompt for this turn. The
      orchestrator composes the personality + tool-schema reminder +
      (optional) waiting-input addendum before passing it in.
    """

    def __init__(self, *, system_content: str) -> None:
        self._system_content = system_content

    @property
    def provider_id(self) -> str:
        return SYSTEM_BLOCK_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        return [
            ContextEntry(
                id=f"{SYSTEM_BLOCK_PROVIDER_ID}:system",
                kind="system_note",
                source="orchestrator",
                token_estimate=len(self._system_content) // 4,
                pinned=True,
                created_at="",
                provider_id=SYSTEM_BLOCK_PROVIDER_ID,
                payload={"role": "system", "content": self._system_content},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        ]
