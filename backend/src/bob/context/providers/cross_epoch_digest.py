"""CrossEpochDigestProvider — inject the freshest cross-epoch digest.

PRD 0006 / issue 0051. After an epoch seals, the cross-epoch digest
is the only sealed-history artefact that flows into the active prompt
— sealed epochs themselves stay in SQLite, never auto-injected. This
provider reads the freshest :class:`CrossEpochDigest` row and emits a
single ``role=system`` :class:`ContextEntry`.

The provider sits between :class:`SystemBlockProvider` and
:class:`RollingSummaryProvider` in the bounded v2 policy:

  [system_block, cross_epoch_digest, rolling_summary, recent_turns, user_message]

PRD 0006 STATE block (issue 0050) will slot in BEFORE
``cross_epoch_digest`` (i.e. right after ``system_block``), so the
final order eventually becomes:

  [system_block, state_block, cross_epoch_digest, rolling_summary,
   recent_turns, user_message]

When the digest store is empty (no epoch has sealed yet) the provider
emits no entry — the assembler skips the block transparently. This is
the early-session common case.
"""

from __future__ import annotations

from collections.abc import Sequence

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry
from bob.context.provider import AssemblyContext
from bob.epoch.digest import CrossEpochDigestStore

#: Stable id used by :class:`bob.context.policy.ContextPolicy` and the
#: assembler's provider registry.
CROSS_EPOCH_DIGEST_PROVIDER_ID = "cross_epoch_digest"


class CrossEpochDigestProvider:
    """Emit the freshest :class:`CrossEpochDigest` as a system-role entry."""

    def __init__(self, *, store: CrossEpochDigestStore) -> None:
        self._store = store

    @property
    def provider_id(self) -> str:
        return CROSS_EPOCH_DIGEST_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        latest = self._store.latest()
        if latest is None or not latest.text:
            return []
        return [
            ContextEntry(
                id=f"{CROSS_EPOCH_DIGEST_PROVIDER_ID}:{latest.id}",
                kind="system_note",
                source="cross_epoch_digest_store",
                token_estimate=latest.token_estimate or (len(latest.text) // 4),
                pinned=True,
                created_at=latest.created_at,
                provider_id=CROSS_EPOCH_DIGEST_PROVIDER_ID,
                payload={"role": "system", "content": latest.text},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        ]
