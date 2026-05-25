"""Tests for :class:`bob.context.providers.rolling_summary.RollingSummaryProvider`."""

from __future__ import annotations

import sqlite3

from bob.context.policy import bounded_v1_policy
from bob.context.prompt_fragments import SUMMARY_BLOCK_HEADER
from bob.context.provider import AssemblyContext
from bob.context.providers.rolling_summary import (
    ROLLING_SUMMARY_PROVIDER_ID,
    RollingSummaryProvider,
)
from bob.context.summariser import SUMMARISER_VERSION
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.rolling_summary_store import RollingSummaryStore


def _fresh_store() -> RollingSummaryStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return RollingSummaryStore(conn)


def test_provider_id_is_stable() -> None:
    provider = RollingSummaryProvider(store=_fresh_store())
    assert provider.provider_id == ROLLING_SUMMARY_PROVIDER_ID


def test_emits_nothing_when_store_empty() -> None:
    provider = RollingSummaryProvider(store=_fresh_store())
    ctx = AssemblyContext(policy=bounded_v1_policy())
    assert list(provider.entries(ctx)) == []


def test_emits_wrapped_latest_summary() -> None:
    store = _fresh_store()
    store.append(
        from_turn=1,
        to_turn=4,
        summariser_version=SUMMARISER_VERSION,
        text="Tom a demandé X puis Y.",
        token_estimate=12,
    )
    provider = RollingSummaryProvider(store=store)
    ctx = AssemblyContext(policy=bounded_v1_policy())

    entries = list(provider.entries(ctx))
    assert len(entries) == 1

    entry = entries[0]
    assert entry.kind == "system_note"
    assert entry.payload["role"] == "system"
    expected_wrap = SUMMARY_BLOCK_HEADER.render(
        from_turn=1,
        to_turn=4,
        summariser_version=SUMMARISER_VERSION,
        summary="Tom a demandé X puis Y.",
    )
    assert entry.payload["content"] == expected_wrap
    assert entry.pinned is True
    assert entry.token_estimate == 12


def test_provider_picks_freshest_summary() -> None:
    store = _fresh_store()
    store.append(
        from_turn=1,
        to_turn=2,
        summariser_version=SUMMARISER_VERSION,
        text="first",
        token_estimate=2,
    )
    store.append(
        from_turn=1,
        to_turn=4,
        summariser_version=SUMMARISER_VERSION,
        text="second",
        token_estimate=4,
    )

    provider = RollingSummaryProvider(store=store)
    entries = list(provider.entries(AssemblyContext(policy=bounded_v1_policy())))
    assert "second" in entries[0].payload["content"]
    assert "first" not in entries[0].payload["content"]
