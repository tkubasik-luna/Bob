"""Provider that reproduces pre-0043 "send the whole thread every turn" behavior.

The pre-0043 orchestrator built each LLM prompt as ``[system, *history]``
where ``history`` was the full persisted Jarvis thread (every user /
assistant / tool message since the last DB wipe). This provider emits
exactly that sequence as :class:`bob.context.entry.ContextEntry` objects so
the :class:`bob.context.assembler.ContextAssembler` can project them back to
the chat-messages list the LLM client expects.

Two important conventions:

1. The system content is passed in at construction time (not pulled from a
   global). The orchestrator builds it differently for the tool-calling
   ``complete()`` call (system + tools addendum + waiting addendum) vs the
   structured ``chat()`` call (system only). Passing the resolved string in
   keeps the provider stateless.

2. The current user turn is appended to the Jarvis store *before* assembly
   (the orchestrator does this on line ~325 today). The provider therefore
   does NOT consume ``ctx.user_message`` — it would otherwise double-emit
   the user message. The field is reserved for later providers
   (``UserMessageProvider`` in slice 0046) that decouple persistence from
   prompt composition.
"""

from __future__ import annotations

from collections.abc import Sequence

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry, ContextEntryKind
from bob.context.provider import AssemblyContext
from bob.jarvis_store import JarvisStore

#: Stable id for this provider — also used as the ``policy_id`` value in
#: :class:`bob.context.policy.ContextPolicy` and as a marker on emitted
#: :class:`ContextEntry` rows.
LEGACY_FULL_HISTORY_PROVIDER_ID = "legacy_full_history"


def _kind_for_role(role: str) -> ContextEntryKind:
    """Map a chat-message role to a :data:`ContextEntryKind` literal."""

    if role == "user":
        return "user_turn"
    if role == "assistant":
        return "assistant_turn"
    # ``tool`` + ``system`` historical messages are folded into ``system_note``
    # for v1. The orchestrator never persisted ``system`` rows pre-0043 and
    # ``tool`` rows were never written either, so this branch is effectively
    # defensive. The legacy provider's job is byte-equality with today's
    # behavior, and today's behavior would simply forward whatever role
    # came back from ``jarvis_store.history()``.
    return "system_note"


class LegacyFullHistoryProvider:
    """Yield the system prompt + the full persisted Jarvis thread, in order.

    Construction args:

    - ``jarvis_store`` — the SQLite-backed store. Read-only access during
      :meth:`entries`.
    - ``system_content`` — the system prompt for this turn (the orchestrator
      composes the right addendum stack before calling assemble).
    """

    def __init__(
        self,
        *,
        jarvis_store: JarvisStore,
        system_content: str,
    ) -> None:
        self._jarvis_store = jarvis_store
        self._system_content = system_content

    @property
    def provider_id(self) -> str:
        return LEGACY_FULL_HISTORY_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        """Return ``[system_entry, *history_entries]`` in chronological order."""

        out: list[ContextEntry] = [
            ContextEntry(
                id=f"{LEGACY_FULL_HISTORY_PROVIDER_ID}:system",
                kind="system_note",
                source="orchestrator",
                token_estimate=len(self._system_content) // 4,
                pinned=True,
                created_at="",
                provider_id=LEGACY_FULL_HISTORY_PROVIDER_ID,
                payload={"role": "system", "content": self._system_content},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        ]
        for idx, msg in enumerate(self._jarvis_store.history()):
            role = msg["role"]
            content = msg["content"]
            out.append(
                ContextEntry(
                    id=f"{LEGACY_FULL_HISTORY_PROVIDER_ID}:history:{idx}",
                    kind=_kind_for_role(role),
                    source="jarvis_store",
                    token_estimate=len(content) // 4,
                    pinned=False,
                    created_at="",
                    provider_id=LEGACY_FULL_HISTORY_PROVIDER_ID,
                    payload={"role": role, "content": content},
                    schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
                )
            )
        return out
