"""RecentTurnsProvider — emit the last K user↔Jarvis turn pairs verbatim.

PRD 0006 / issue 0046. Bounded context replaces the legacy "send the whole
thread every turn" with a small window of verbatim recent turns plus a
rolling summary over everything older. This provider owns the verbatim
window.

Construction args:

- ``jarvis_store`` — read-only access to the persisted thread.
- ``include_live_user_message`` — when ``True`` (default ``False``), the
  in-progress user message persisted just before assembly is included in
  the window. The bounded policy uses ``False`` because :class:`UserMessageProvider`
  emits the live message as a separate trailing entry. The provider is
  exposed with the flag so future policies that *do* fold the live turn
  into the window can re-use the same code.

Behavior:

- The provider reads ``ContextPolicy.recent_turns_window`` (K, default
  ``3``) at :meth:`entries` time. K refers to user↔assistant turn *pairs*;
  the provider emits up to ``2 * K`` :class:`ContextEntry` rows, in
  chronological order.
- Pairing is forgiving: we walk the persisted history backwards collecting
  entries until we have ``2 * K`` rows (counting both roles), then emit
  them oldest-first. A trailing un-paired user turn (last entry is a
  ``user`` row, like when the live user message is already persisted)
  drops cleanly out via ``include_live_user_message=False``.
- The provider never overlaps with :class:`RollingSummaryProvider`. The
  exact older/recent split is computed at the orchestrator level by
  passing the persisted history-length to both providers; this provider
  simply consumes ``K`` from the policy.
"""

from __future__ import annotations

from collections.abc import Sequence

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry, ContextEntryKind
from bob.context.provider import AssemblyContext
from bob.jarvis_store import JarvisStore, Message

#: Stable id.
RECENT_TURNS_PROVIDER_ID = "recent_turns"

#: Default window when :attr:`ContextPolicy.recent_turns_window` is ``None``.
DEFAULT_RECENT_TURNS_WINDOW = 3


def _kind_for_role(role: str) -> ContextEntryKind:
    if role == "user":
        return "user_turn"
    if role == "assistant":
        return "assistant_turn"
    return "system_note"


class RecentTurnsProvider:
    """Emit the most recent ``2 * K`` history rows in chronological order."""

    def __init__(
        self,
        *,
        jarvis_store: JarvisStore,
        include_live_user_message: bool = False,
    ) -> None:
        self._jarvis_store = jarvis_store
        self._include_live = include_live_user_message

    @property
    def provider_id(self) -> str:
        return RECENT_TURNS_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        window = ctx.policy.recent_turns_window
        if window is None or window <= 0:
            window = DEFAULT_RECENT_TURNS_WINDOW
        # We count entries, not pairs: ``2 * window`` rows covers ``window``
        # user↔assistant pairs in the common case. When a tool / system row
        # sneaks in we still emit at most ``2 * window`` entries from the
        # tail, preserving the "bounded" guarantee.
        max_rows = 2 * window

        history = list(self._jarvis_store.history())
        # Drop the trailing user turn if present — the orchestrator
        # appended it before assembly and ``UserMessageProvider`` emits it.
        if not self._include_live and history and history[-1].get("role") == "user":
            history = history[:-1]

        # Recent slice in chronological order.
        recent = history[-max_rows:] if max_rows < len(history) else history

        out: list[ContextEntry] = []
        for idx, msg in enumerate(recent):
            role = msg["role"]
            content = msg["content"]
            out.append(
                ContextEntry(
                    id=f"{RECENT_TURNS_PROVIDER_ID}:{idx}",
                    kind=_kind_for_role(role),
                    source="jarvis_store",
                    token_estimate=len(content) // 4,
                    pinned=False,
                    created_at="",
                    provider_id=RECENT_TURNS_PROVIDER_ID,
                    payload={"role": role, "content": content},
                    schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
                )
            )
        return out

    def older_history(self, *, window: int | None = None) -> list[Message]:
        """Return the persisted history entries NOT covered by the recent window.

        Helper used by :class:`bob.context.providers.rolling_summary.RollingSummaryProvider`
        and by the orchestrator's summariser scheduler. Pure / read-only.

        ``window`` overrides the policy value when callers know exactly how
        many entries the recent slice will consume (they may want to scope
        the rolling summary to a specific from/to range).
        """

        actual_window = window if window is not None else DEFAULT_RECENT_TURNS_WINDOW
        if actual_window <= 0:
            actual_window = DEFAULT_RECENT_TURNS_WINDOW
        max_rows = 2 * actual_window
        history = list(self._jarvis_store.history())
        if not self._include_live and history and history[-1].get("role") == "user":
            history = history[:-1]
        if max_rows >= len(history):
            return []
        return history[: len(history) - max_rows]
