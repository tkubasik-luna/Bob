"""Pure :class:`ContextAssembler` — composes providers into a chat-messages list.

The assembler is intentionally a deep, side-effect-free module: given a list
of :class:`bob.context.provider.ContextProvider` instances and a
:class:`bob.context.policy.ContextPolicy`, it produces the exact same shape
that the orchestrator used to feed to :meth:`bob.llm_client.LLMClient.complete`
or :meth:`bob.llm_client.LLMClient.chat`.

Issue 0043 only wires
:class:`bob.context.providers.legacy_full_history.LegacyFullHistoryProvider`,
which already returns entries in role-tagged form (``system`` first, then
``user``/``assistant`` turns in chronological order). The assembler therefore
just iterates ``provider.entries(ctx)`` in policy order and projects each
entry to a ``{"role": ..., "content": ...}`` chat-message dict.

Later slices replace the legacy provider with multiple bounded providers
(``SystemBlockProvider``, ``StateBlockProvider``, ``RollingSummaryProvider``,
``RecentTurnsProvider``, ``UserMessageProvider``) that each emit a single
role-tagged entry, and the same iteration logic keeps working. That is the
whole point of the foundation: the assembler API stays stable while the
provider mix evolves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bob.context.entry import ContextEntry
from bob.context.policy import ContextPolicy
from bob.context.provider import AssemblyContext, ContextProvider


class ContextAssemblerError(RuntimeError):
    """Raised when an entry has a payload shape the assembler cannot project."""


def _entry_to_chat_message(entry: ContextEntry) -> dict[str, Any]:
    """Project a :class:`ContextEntry` to a ``{"role": ..., "content": ...}`` dict.

    Issue 0043 only handles entries whose payload already carries a ``role``
    and a ``content`` field (this is the contract the legacy provider
    follows). Later slices that emit synthetic entries (STATE block, rolling
    summary, …) will add their own projection by going through the
    ``"chat_message"`` helper or by extending this dispatch.
    """

    payload = entry.payload
    if not isinstance(payload, Mapping):
        raise ContextAssemblerError(
            f"ContextEntry payload must be a mapping (entry.id={entry.id!r}, "
            f"provider={entry.provider_id!r})"
        )
    role = payload.get("role")
    if not isinstance(role, str) or not role:
        raise ContextAssemblerError(
            f"ContextEntry payload missing 'role' (entry.id={entry.id!r}, "
            f"provider={entry.provider_id!r})"
        )
    content = payload.get("content")
    if not isinstance(content, str):
        raise ContextAssemblerError(
            f"ContextEntry payload 'content' must be a string (entry.id={entry.id!r}, "
            f"provider={entry.provider_id!r})"
        )
    return {"role": role, "content": content}


class ContextAssembler:
    """Compose a list of providers into a chat-messages list.

    Construction is a one-shot wiring step: pass the providers (mapped by
    their ``provider_id``) and a :class:`ContextPolicy`. Each call to
    :meth:`assemble` iterates the policy's ``provider_ids`` in order and
    concatenates the entries each provider yields.

    The assembler is pure: no I/O, no time, no randomness. It only reads
    the providers passed in at construction. This makes it safe to snapshot
    in golden-prompt tests (see ``tests/_harness/golden_prompt.py``).
    """

    def __init__(
        self,
        *,
        providers: Sequence[ContextProvider],
        policy: ContextPolicy,
    ) -> None:
        self._policy = policy
        self._providers_by_id: dict[str, ContextProvider] = {}
        for provider in providers:
            pid = provider.provider_id
            if pid in self._providers_by_id:
                raise ValueError(f"Duplicate provider_id in registry: {pid!r}")
            self._providers_by_id[pid] = provider

    @property
    def policy(self) -> ContextPolicy:
        return self._policy

    def assemble(self, *, user_message: str | None = None) -> list[dict[str, Any]]:
        """Return the chat messages list to feed to the LLM for this turn.

        ``user_message`` is the live user input for the in-progress turn. The
        legacy provider already pulls the full thread (including the user
        message which is appended to the Jarvis store *before* assembly), so
        it does not consume ``user_message``. Later slices use it via
        :class:`bob.context.provider.AssemblyContext`.
        """

        ctx = AssemblyContext(policy=self._policy, user_message=user_message)
        entries = self.collect_entries(ctx)
        return [_entry_to_chat_message(entry) for entry in entries]

    def collect_entries(self, ctx: AssemblyContext) -> list[ContextEntry]:
        """Return the raw :class:`ContextEntry` list, in policy order.

        Exposed separately from :meth:`assemble` so tests can inspect the
        provider stream without going through the chat-message projection.
        """

        out: list[ContextEntry] = []
        for pid in self._policy.provider_ids:
            try:
                provider = self._providers_by_id[pid]
            except KeyError as exc:
                raise ContextAssemblerError(
                    f"ContextPolicy refers to provider_id={pid!r} "
                    f"but no such provider was registered "
                    f"(known: {sorted(self._providers_by_id)})"
                ) from exc
            out.extend(provider.entries(ctx))
        return out
