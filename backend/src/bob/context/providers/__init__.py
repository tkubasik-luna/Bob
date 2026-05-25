"""Concrete :class:`bob.context.provider.ContextProvider` implementations.

Issue 0043 only shipped :class:`legacy_full_history.LegacyFullHistoryProvider`.
Issue 0046 adds the bounded providers:

* :class:`system_block.SystemBlockProvider` — the resolved system prompt
  for the current turn.
* :class:`rolling_summary.RollingSummaryProvider` — the persisted
  rolling summary over older turns.
* :class:`recent_turns.RecentTurnsProvider` — the last K verbatim
  user/assistant turn pairs.
* :class:`user_message.UserMessageProvider` — the live in-progress user
  turn injected via :class:`AssemblyContext`.

The legacy provider remains importable as the safety-net regression
target. The orchestrator default policy now wires the bounded providers.
"""

from __future__ import annotations

from bob.context.providers.cross_epoch_digest import CrossEpochDigestProvider
from bob.context.providers.legacy_full_history import LegacyFullHistoryProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.system_block import SystemBlockProvider
from bob.context.providers.user_message import UserMessageProvider

__all__ = [
    "CrossEpochDigestProvider",
    "LegacyFullHistoryProvider",
    "RecentTurnsProvider",
    "RollingSummaryProvider",
    "SystemBlockProvider",
    "UserMessageProvider",
]
