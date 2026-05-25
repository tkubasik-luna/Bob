"""RollingSummaryProvider — emit a single ``role=system`` summary block.

PRD 0006 / issue 0046. The bounded ``ContextPolicy`` injects a summary of
the older turns ahead of the recent window. This provider reads the
freshest :class:`StoredRollingSummary` from :class:`RollingSummaryStore`
and emits a single :class:`ContextEntry` carrying the summary text wrapped
by :data:`bob.context.prompt_fragments.SUMMARY_BLOCK_HEADER`.

Generation / regeneration of the summary is the orchestrator's concern
(see :func:`bob.context.summary_pipeline.maybe_regenerate_rolling_summary`).
The provider is pure: it never calls the LLM. It simply projects the
latest persisted row into a :class:`ContextEntry`. When the store is
empty (early session, before the first regeneration) the provider emits
no entry — the assembler skips the block transparently.
"""

from __future__ import annotations

from collections.abc import Sequence

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry
from bob.context.prompt_fragments import SUMMARY_BLOCK_HEADER
from bob.context.provider import AssemblyContext
from bob.rolling_summary_store import RollingSummaryStore

#: Stable id.
ROLLING_SUMMARY_PROVIDER_ID = "rolling_summary"


class RollingSummaryProvider:
    """Emit the freshest persisted :class:`StoredRollingSummary` as a system entry."""

    def __init__(self, *, store: RollingSummaryStore) -> None:
        self._store = store

    @property
    def provider_id(self) -> str:
        return ROLLING_SUMMARY_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        latest = self._store.latest()
        if latest is None:
            return []
        wrapped = SUMMARY_BLOCK_HEADER.render(
            from_turn=latest.from_turn,
            to_turn=latest.to_turn,
            summariser_version=latest.summariser_version,
            summary=latest.text,
        )
        return [
            ContextEntry(
                id=f"{ROLLING_SUMMARY_PROVIDER_ID}:{latest.id}",
                kind="system_note",
                source="rolling_summary_store",
                token_estimate=latest.token_estimate or (len(wrapped) // 4),
                pinned=True,
                created_at=latest.created_at,
                provider_id=ROLLING_SUMMARY_PROVIDER_ID,
                payload={"role": "system", "content": wrapped},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        ]
