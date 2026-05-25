"""Concrete :class:`bob.context.provider.ContextProvider` implementations.

Issue 0043 only ships :class:`legacy_full_history.LegacyFullHistoryProvider`,
which reproduces today's "send the whole thread every turn" behavior. Later
slices add ``SystemBlockProvider``, ``StateBlockProvider``,
``RollingSummaryProvider``, ``RecentTurnsProvider``, ``UserMessageProvider``
in this same package.
"""

from __future__ import annotations

from bob.context.providers.legacy_full_history import LegacyFullHistoryProvider

__all__ = ["LegacyFullHistoryProvider"]
