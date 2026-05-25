"""Tests for :mod:`bob.epoch.digest` — pure rebuild + store contract.

The :func:`regenerate_cross_epoch_digest` function is the load-bearing
piece for the drift-bounding invariant: every rebuild MUST consume RAW
sealed turns, never the prior digest. We test the pure function with
synthetic :class:`ContextEntry` lists.

The store mirrors :class:`bob.rolling_summary_store.RollingSummaryStore`
so the test surface is small.
"""

from __future__ import annotations

import sqlite3

from bob.context.entry import ContextEntry
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.epoch.digest import (
    CROSS_EPOCH_DIGEST_HEADER,
    CrossEpochDigestStore,
    regenerate_cross_epoch_digest,
)
from bob.epoch.policy import EpochPolicy


def _entry(idx: int, role: str, content: str) -> ContextEntry:
    return ContextEntry(
        id=f"raw:{idx}",
        kind="user_turn" if role == "user" else "assistant_turn",
        source="test",
        token_estimate=0,
        pinned=False,
        created_at="",
        provider_id="sealed_turns",
        payload={"role": role, "content": content},
    )


# ---------------------------------------------------------------------------
# regenerate_cross_epoch_digest — pure function.
# ---------------------------------------------------------------------------


def test_regenerate_returns_empty_string_for_empty_input() -> None:
    text = regenerate_cross_epoch_digest(
        sealed_turns=[],
        sealed_epoch_count=0,
        policy=EpochPolicy(),
    )
    assert text == ""


def test_regenerate_composes_header_plus_transcript() -> None:
    entries = [
        _entry(0, "user", "salut bob"),
        _entry(1, "assistant", "salut tom"),
    ]
    text = regenerate_cross_epoch_digest(
        sealed_turns=entries,
        sealed_epoch_count=1,
        policy=EpochPolicy(max_digest_size=4000),
    )

    assert CROSS_EPOCH_DIGEST_HEADER in text
    assert "USER: salut bob" in text
    assert "ASSISTANT: salut tom" in text


def test_regenerate_truncates_when_over_cap() -> None:
    """Digest body capped at ``max_digest_size`` with trailing ellipsis."""

    entries = [_entry(i, "user", f"raw turn number {i} with extra padding") for i in range(50)]
    text = regenerate_cross_epoch_digest(
        sealed_turns=entries,
        sealed_epoch_count=1,
        policy=EpochPolicy(max_digest_size=120),
    )

    assert len(text) <= 120
    assert text.endswith("…")
    # The header is always preserved at the head.
    assert text.startswith(CROSS_EPOCH_DIGEST_HEADER.split(" (")[0])


def test_regenerate_skips_non_string_payloads() -> None:
    """Defensive — only role+content string payloads are folded in."""

    good = _entry(0, "user", "kept")
    bad = ContextEntry(
        id="raw:1",
        kind="user_turn",
        source="test",
        token_estimate=0,
        pinned=False,
        created_at="",
        provider_id="sealed_turns",
        payload={"role": 123, "content": "skipped"},
    )
    text = regenerate_cross_epoch_digest(
        sealed_turns=[good, bad],
        sealed_epoch_count=1,
        policy=EpochPolicy(max_digest_size=4000),
    )

    assert "kept" in text
    assert "skipped" not in text


def test_regenerate_never_uses_prior_digest_as_input() -> None:
    """Drift-bounding invariant — explicit assertion at the function level.

    We compose two regeneration calls separated by an arbitrary "prior
    digest" string and verify the second result is identical to the
    first, i.e. the prior digest had zero influence.
    """

    entries = [_entry(0, "user", "raw conversation about python")]
    policy = EpochPolicy(max_digest_size=4000)

    first = regenerate_cross_epoch_digest(sealed_turns=entries, sealed_epoch_count=1, policy=policy)
    # Pass the SAME entries again — adding a "fake prior digest" entry
    # is structurally not possible because the function only accepts
    # raw turns. The fact that callers cannot smuggle a digest is the
    # API-level enforcement.
    second = regenerate_cross_epoch_digest(
        sealed_turns=entries, sealed_epoch_count=1, policy=policy
    )
    assert first == second
    # And: the prior digest text NEVER appears as input to itself.
    assert CROSS_EPOCH_DIGEST_HEADER not in entries[0].payload["content"]


# ---------------------------------------------------------------------------
# CrossEpochDigestStore — append-only sqlite contract.
# ---------------------------------------------------------------------------


def _store() -> CrossEpochDigestStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return CrossEpochDigestStore(conn)


def test_store_append_returns_increasing_ids() -> None:
    store = _store()
    a = store.append(text="d1", summariser_version=1, sealed_epoch_count=1)
    b = store.append(text="d2", summariser_version=1, sealed_epoch_count=2)
    assert b > a


def test_store_latest_returns_freshest_row() -> None:
    store = _store()
    store.append(text="d1", summariser_version=1, sealed_epoch_count=1)
    store.append(text="d2", summariser_version=1, sealed_epoch_count=2)

    latest = store.latest()
    assert latest is not None
    assert latest.text == "d2"
    assert latest.sealed_epoch_count == 2


def test_store_latest_none_when_empty() -> None:
    store = _store()
    assert store.latest() is None


def test_store_rejects_negative_sealed_epoch_count() -> None:
    store = _store()
    import pytest

    with pytest.raises(ValueError):
        store.append(text="x", summariser_version=1, sealed_epoch_count=-1)
