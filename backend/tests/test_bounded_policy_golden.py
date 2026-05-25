"""Golden snapshot tests for the bounded :class:`ContextPolicy`.

Pins the assembled prompt shape under the bounded providers for three
fixtures:

* ``bounded_simple`` — short history, no rolling summary yet (early
  session). Uses ``bounded_v1`` for the byte-for-byte regression
  against issue 0046.
* ``bounded_with_summary`` — long enough that the summariser pipeline
  populates the rolling-summary block. Uses ``bounded_v1``.
* ``bounded_v2_with_digest`` — additionally includes the cross-epoch
  digest block (issue 0051). Asserts the digest slots between the
  system block and the rolling summary.

These snapshots are the user-visible "before/after" diff the PRD wants
committed alongside this slice. Any future change to the bounded prompt
structure will fail these tests until ``BOB_UPDATE_SNAPSHOTS=1`` is set,
forcing a conscious review.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import pytest

from bob.context.assembler import ContextAssembler
from bob.context.entry import ContextEntry
from bob.context.policy import bounded_v1_policy, bounded_v2_policy
from bob.context.providers.cross_epoch_digest import CrossEpochDigestProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.system_block import SystemBlockProvider
from bob.context.providers.user_message import UserMessageProvider
from bob.context.summariser import SUMMARISER_VERSION, RollingSummary
from bob.context.summary_pipeline import maybe_regenerate_rolling_summary
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.epoch.digest import CrossEpochDigestStore
from bob.jarvis_store import JarvisStore
from bob.rolling_summary_store import RollingSummaryStore

from ._harness.golden_prompt import (
    assert_matches_snapshot,
    load_transcript_fixture,
    seed_history,
)


class _StaticSummariser:
    """Returns a fixed digest so the snapshot is deterministic."""

    async def summarise(
        self,
        *,
        older_turns: Sequence[ContextEntry],
        from_turn: int,
        to_turn: int,
    ) -> RollingSummary | None:
        if not older_turns:
            return None
        return RollingSummary(
            text="Échanges précédents : Python, Rust, Go, Kotlin évoqués.",
            summariser_version=SUMMARISER_VERSION,
            from_turn=from_turn,
            to_turn=to_turn,
            raw_turn_count=len(older_turns),
        )


def _bounded_messages(
    *,
    jarvis_store: JarvisStore,
    summary_store: RollingSummaryStore,
    system_content: str,
    user_message: str,
) -> list[dict[str, str]]:
    from bob.context.provider import ContextProvider

    providers: list[ContextProvider] = [
        SystemBlockProvider(system_content=system_content),
        RollingSummaryProvider(store=summary_store),
        RecentTurnsProvider(jarvis_store=jarvis_store),
        UserMessageProvider(),
    ]
    assembler = ContextAssembler(providers=providers, policy=bounded_v1_policy())
    return assembler.assemble(user_message=user_message)


def _stores() -> tuple[JarvisStore, RollingSummaryStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return JarvisStore(conn), RollingSummaryStore(conn)


def test_golden_snapshot_bounded_simple() -> None:
    """No rolling summary block when the older slice is empty."""

    transcript = load_transcript_fixture("bounded_simple")
    jarvis_store, summary_store = _stores()
    seed_history(jarvis_store, transcript)

    messages = _bounded_messages(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        system_content=transcript["system_content"],
        user_message=transcript["pending_user_message"],
    )

    assert_matches_snapshot(messages, "bounded_simple")


@pytest.mark.asyncio
async def test_golden_snapshot_bounded_with_summary() -> None:
    """Rolling summary block appears once the older slice has been summarised."""

    transcript = load_transcript_fixture("bounded_with_summary")
    jarvis_store, summary_store = _stores()
    seed_history(jarvis_store, transcript)

    await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=_StaticSummariser(),
        recent_window=2,
        trigger_delta=1,
    )

    messages = _bounded_messages(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        system_content=transcript["system_content"],
        user_message=transcript["pending_user_message"],
    )

    assert_matches_snapshot(messages, "bounded_with_summary")


def _bounded_v2_messages(
    *,
    jarvis_store: JarvisStore,
    summary_store: RollingSummaryStore,
    digest_store: CrossEpochDigestStore,
    system_content: str,
    user_message: str,
) -> list[dict[str, str]]:
    from bob.context.provider import ContextProvider

    providers: list[ContextProvider] = [
        SystemBlockProvider(system_content=system_content),
        CrossEpochDigestProvider(store=digest_store),
        RollingSummaryProvider(store=summary_store),
        RecentTurnsProvider(jarvis_store=jarvis_store),
        UserMessageProvider(),
    ]
    assembler = ContextAssembler(providers=providers, policy=bounded_v2_policy())
    return assembler.assemble(user_message=user_message)


@pytest.mark.asyncio
async def test_golden_snapshot_bounded_v2_with_digest() -> None:
    """v2 policy with both a rolling summary and a cross-epoch digest persisted.

    Pins the provider order for issue 0051: system block, cross-epoch
    digest, rolling summary, recent turns, live user message. The
    digest sits between the system block and the rolling summary.
    """

    transcript = load_transcript_fixture("bounded_with_summary")
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    summary_store = RollingSummaryStore(conn)
    digest_store = CrossEpochDigestStore(conn)
    seed_history(jarvis_store, transcript)

    await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=_StaticSummariser(),
        recent_window=2,
        trigger_delta=1,
    )
    digest_store.append(
        text="Synthèse des époques passées (reconstituée à partir des tours bruts) :\n"
        "- USER: T0: vieux contexte avant l'epoch 0",
        summariser_version=1,
        sealed_epoch_count=1,
        token_estimate=12,
    )

    messages = _bounded_v2_messages(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        digest_store=digest_store,
        system_content=transcript["system_content"],
        user_message=transcript["pending_user_message"],
    )

    assert_matches_snapshot(messages, "bounded_v2_with_digest")
