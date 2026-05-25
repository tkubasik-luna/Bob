"""Glue between :class:`Summariser` and :class:`RollingSummaryStore`.

PRD 0006 / issue 0046. The orchestrator calls
:func:`maybe_regenerate_rolling_summary` after persisting a user turn but
before assembling the prompt. The function:

1. Inspects the persisted Jarvis history and the policy's
   ``recent_turns_window`` to compute the RAW older turns slice that sits
   outside the recent window.
2. Decides whether the slice grew large enough since the last persisted
   summary to warrant regeneration. The trigger is conservative —
   regenerate whenever the older slice has at least ``trigger_delta`` more
   turns than the previous summary covered (default ``2``). This keeps
   regeneration costs bounded while still tracking the window.
3. Hands the RAW older turns to the injected :class:`Summariser`,
   persists the result, and returns the freshly stored row.

The pipeline is intentionally pure aside from the store / summariser
interactions: no I/O on the LLM client itself, no time-based triggers.
That keeps the long-session smoke test deterministic.

The summariser MUST always see RAW older turns — never the prior digest.
This is the central drift-bounding invariant of the PRD. The pipeline
materialises that invariant by reading raw ``ContextEntry``-shaped rows
out of :class:`bob.jarvis_store.JarvisStore` and passing them straight to
:meth:`Summariser.summarise`. The summariser callable receives no
reference to :class:`RollingSummaryStore`.
"""

from __future__ import annotations

from collections.abc import Sequence

from bob.context.entry import (
    CONTEXT_ENTRY_SCHEMA_VERSION,
    ContextEntry,
    ContextEntryKind,
)
from bob.context.summariser import RollingSummary, Summariser
from bob.jarvis_store import JarvisStore, Message
from bob.rolling_summary_store import RollingSummaryStore, StoredRollingSummary

#: Default minimum number of additional older turns required before a
#: regeneration is triggered. Picked so the smoke test plateaus around
#: turn 30 with a recent window of 3 (≈ 6 verbatim rows).
DEFAULT_TRIGGER_DELTA = 2


def _history_for_pipeline(
    jarvis_store: JarvisStore,
    *,
    include_live_user_message: bool,
) -> list[Message]:
    """Return the persisted history, optionally trimming the trailing user turn."""

    history = list(jarvis_store.history())
    if not include_live_user_message and history and history[-1].get("role") == "user":
        history = history[:-1]
    return history


def _older_slice(
    history: Sequence[Message],
    *,
    recent_window: int,
) -> list[Message]:
    """Return entries NOT covered by the recent ``2*K``-row window."""

    max_recent_rows = 2 * recent_window
    if max_recent_rows >= len(history):
        return []
    return list(history[: len(history) - max_recent_rows])


def _to_context_entries(messages: Sequence[Message]) -> list[ContextEntry]:
    """Project raw store rows to RAW :class:`ContextEntry` items for the summariser.

    The summariser API is :class:`ContextEntry`-shaped to keep one mental
    model — future kinds (``task_completed``…) flow through the same
    summarisation pipeline.
    """

    out: list[ContextEntry] = []
    for idx, msg in enumerate(messages):
        role = msg["role"]
        content = msg["content"]
        kind: ContextEntryKind = "user_turn" if role == "user" else "assistant_turn"
        out.append(
            ContextEntry(
                id=f"raw_older:{idx}",
                kind=kind,
                source="jarvis_store",
                token_estimate=len(content) // 4,
                pinned=False,
                created_at="",
                provider_id="raw_older",
                payload={"role": role, "content": content},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        )
    return out


async def maybe_regenerate_rolling_summary(
    *,
    jarvis_store: JarvisStore,
    summary_store: RollingSummaryStore,
    summariser: Summariser,
    recent_window: int,
    trigger_delta: int = DEFAULT_TRIGGER_DELTA,
    include_live_user_message: bool = False,
    current_epoch_id: int = 0,
) -> StoredRollingSummary | None:
    """Regenerate the rolling summary if the older slice has grown enough.

    Returns the newly persisted row when regeneration happened, the
    previously latest row when no regeneration was needed (still
    non-``None`` so callers can introspect), or ``None`` when there is
    nothing to summarise yet (early session, older slice empty).

    ``current_epoch_id`` (PRD 0006 / issue 0051) is stamped on the
    persisted row so the :class:`bob.epoch.manager.EpochManager` can
    distinguish "current epoch's rolling summary" from earlier sealed
    summaries. Defaults to ``0`` to preserve pre-0051 behavior.

    See module docstring for the trigger rule.
    """

    if recent_window <= 0:
        recent_window = 3

    history = _history_for_pipeline(
        jarvis_store, include_live_user_message=include_live_user_message
    )
    older = _older_slice(history, recent_window=recent_window)
    if not older:
        return summary_store.latest_for_epoch(current_epoch_id) or summary_store.latest()

    older_to_turn = len(older)  # 1-indexed inclusive bound (count of older rows)
    latest = summary_store.latest_for_epoch(current_epoch_id)
    if latest is not None and (older_to_turn - latest.to_turn) < trigger_delta:
        return latest

    older_entries = _to_context_entries(older)
    result: RollingSummary | None = await summariser.summarise(
        older_turns=older_entries,
        from_turn=1,
        to_turn=older_to_turn,
    )
    if result is None:
        return latest

    summary_store.append(
        from_turn=result.from_turn,
        to_turn=result.to_turn,
        summariser_version=result.summariser_version,
        text=result.text,
        token_estimate=len(result.text) // 4,
        epoch_id=current_epoch_id,
    )
    return summary_store.latest_for_epoch(current_epoch_id)
