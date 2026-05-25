"""Epoch sealing + retrieval stub (PRD 0006 / issue 0051).

Conversation memory survives arbitrarily long sessions by sealing the
rolling summary into epochs. The orchestrator runs the bounded policy
against the *current* epoch (recent turns + current rolling summary)
plus a cross-epoch digest rebuilt from RAW sealed turns at every seal.

Public surface:

- :class:`EpochPolicy` — token threshold + summariser model id + prompt
  version + max digest size.
- :class:`EpochManager` — deterministic decision/apply functions for
  "should we seal?" and the actual seal procedure.
- :class:`CrossEpochDigest` / :class:`CrossEpochDigestStore` — append-only
  store for the freshest digest text.
- :func:`regenerate_cross_epoch_digest` — pure function rebuilding the
  digest from RAW sealed turns (never from prior digests).
- :class:`RetrievalAPI` — ``recall(query) -> []`` stub with structured
  observability so the read path is honest from day one.

Sealed epochs stay in SQLite, never auto-injected. Active context per
turn is composed by :func:`bob.context.policy.bounded_v2_policy`.
"""

from __future__ import annotations

from bob.epoch.digest import (
    CrossEpochDigest,
    CrossEpochDigestStore,
    regenerate_cross_epoch_digest,
)
from bob.epoch.manager import EpochManager, SealDecision
from bob.epoch.policy import DEFAULT_EPOCH_POLICY, EpochPolicy
from bob.epoch.retrieval import RetrievalAPI

__all__ = [
    "DEFAULT_EPOCH_POLICY",
    "CrossEpochDigest",
    "CrossEpochDigestStore",
    "EpochManager",
    "EpochPolicy",
    "RetrievalAPI",
    "SealDecision",
    "regenerate_cross_epoch_digest",
]
