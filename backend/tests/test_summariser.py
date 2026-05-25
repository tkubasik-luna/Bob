"""Tests for :mod:`bob.context.summariser` and the summary pipeline.

Key assertions:

* The :class:`Summariser` is deterministic for the deterministic impl.
* :data:`SUMMARISER_VERSION` is stamped on every result.
* :func:`maybe_regenerate_rolling_summary` always feeds RAW older turns to
  the summariser — never the prior digest — across N consecutive
  regenerations. This is the central drift-bounding invariant of issue
  0046.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import pytest

from bob.context.entry import ContextEntry
from bob.context.summariser import (
    SUMMARISER_VERSION,
    FixedTextSummariser,
    LLMSummariser,
    RollingSummary,
    render_transcript_for_summary,
)
from bob.context.summary_pipeline import maybe_regenerate_rolling_summary
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.rolling_summary_store import RollingSummaryStore


def _setup() -> tuple[JarvisStore, RollingSummaryStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return JarvisStore(conn), RollingSummaryStore(conn)


def _seed_pairs(store: JarvisStore, count: int) -> None:
    for idx in range(count):
        store.append("user", f"u{idx}")
        store.append("assistant", f"a{idx}")


# ---------------------------------------------------------------------------
# FixedTextSummariser — deterministic, version-stamped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_summariser_is_deterministic() -> None:
    summariser = FixedTextSummariser()
    entries = [
        ContextEntry(
            id="raw:0",
            kind="user_turn",
            source="test",
            token_estimate=1,
            pinned=False,
            created_at="",
            provider_id="raw_older",
            payload={"role": "user", "content": "hello"},
        ),
        ContextEntry(
            id="raw:1",
            kind="assistant_turn",
            source="test",
            token_estimate=1,
            pinned=False,
            created_at="",
            provider_id="raw_older",
            payload={"role": "assistant", "content": "hi"},
        ),
    ]
    first = await summariser.summarise(older_turns=entries, from_turn=1, to_turn=2)
    second = await summariser.summarise(older_turns=entries, from_turn=1, to_turn=2)
    assert first is not None
    assert second is not None
    assert first == second
    assert first.summariser_version == SUMMARISER_VERSION
    assert first.from_turn == 1
    assert first.to_turn == 2
    assert "hello" in first.text
    assert "hi" in first.text


@pytest.mark.asyncio
async def test_fixed_summariser_returns_none_for_empty_input() -> None:
    summariser = FixedTextSummariser()
    result = await summariser.summarise(older_turns=[], from_turn=1, to_turn=0)
    assert result is None


@pytest.mark.asyncio
async def test_render_transcript_skips_non_string_payloads() -> None:
    entries = [
        ContextEntry(
            id="raw:0",
            kind="user_turn",
            source="test",
            token_estimate=0,
            pinned=False,
            created_at="",
            provider_id="raw_older",
            payload={"role": "user", "content": "ok"},
        ),
        ContextEntry(
            id="raw:1",
            kind="user_turn",
            source="test",
            token_estimate=0,
            pinned=False,
            created_at="",
            provider_id="raw_older",
            payload={"role": 123, "content": "skipped"},
        ),
    ]
    rendered = render_transcript_for_summary(entries)
    assert "ok" in rendered
    assert "skipped" not in rendered


# ---------------------------------------------------------------------------
# LLMSummariser — wraps a callable, stamps version, RAW transcript handed in.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_summariser_passes_raw_transcript_to_callable() -> None:
    captured: list[list[dict[str, str]]] = []

    async def fake_chat(messages: list[dict[str, str]]) -> str:
        captured.append(messages)
        return "rendered summary"

    summariser = LLMSummariser(chat=fake_chat)
    entries = [
        ContextEntry(
            id=f"raw:{i}",
            kind="user_turn",
            source="t",
            token_estimate=0,
            pinned=False,
            created_at="",
            provider_id="raw_older",
            payload={"role": "user", "content": f"u{i}"},
        )
        for i in range(3)
    ]
    result = await summariser.summarise(older_turns=entries, from_turn=1, to_turn=3)
    assert result is not None
    assert result.text == "rendered summary"
    assert result.summariser_version == SUMMARISER_VERSION
    assert result.from_turn == 1
    assert result.to_turn == 3

    assert len(captured) == 1
    messages = captured[0]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    user_body = messages[1]["content"]
    for i in range(3):
        assert f"u{i}" in user_body


@pytest.mark.asyncio
async def test_llm_summariser_falls_back_for_empty_response() -> None:
    async def empty_chat(_messages: list[dict[str, str]]) -> str:
        return "   "

    summariser = LLMSummariser(chat=empty_chat)
    entries = [
        ContextEntry(
            id="raw:0",
            kind="user_turn",
            source="t",
            token_estimate=0,
            pinned=False,
            created_at="",
            provider_id="raw_older",
            payload={"role": "user", "content": "u0"},
        )
    ]
    result = await summariser.summarise(older_turns=entries, from_turn=1, to_turn=1)
    assert result is not None
    assert result.text  # non-empty fallback marker


# ---------------------------------------------------------------------------
# Pipeline — RAW older turns flow N times, never the prior digest.
# ---------------------------------------------------------------------------


class _RecordingSummariser:
    """Records every input payload + returns deterministic output."""

    def __init__(self) -> None:
        self.calls: list[list[ContextEntry]] = []
        self.from_turns: list[int] = []
        self.to_turns: list[int] = []

    async def summarise(
        self,
        *,
        older_turns: Sequence[ContextEntry],
        from_turn: int,
        to_turn: int,
    ) -> RollingSummary | None:
        self.calls.append(list(older_turns))
        self.from_turns.append(from_turn)
        self.to_turns.append(to_turn)
        body = "\n".join(e.payload.get("content", "") for e in older_turns)
        return RollingSummary(
            text=f"summary({from_turn}->{to_turn})\n{body}",
            summariser_version=SUMMARISER_VERSION,
            from_turn=from_turn,
            to_turn=to_turn,
            raw_turn_count=len(older_turns),
        )


@pytest.mark.asyncio
async def test_pipeline_feeds_raw_older_turns_every_regeneration() -> None:
    """N regenerations all receive RAW older turns, never the prior digest.

    Drift-bounding invariant from PRD 0006 / issue 0046. We seed the
    pipeline with a growing history and assert that every recorded call's
    input is the RAW turns (length grows with each regeneration) and that
    the previously-persisted digest's text is never present in subsequent
    input.
    """

    jarvis_store, summary_store = _setup()
    summariser = _RecordingSummariser()

    # Seed enough turns to trigger the first regeneration. Recent window
    # K=2 → 4 recent rows kept verbatim.
    _seed_pairs(jarvis_store, 6)  # 12 rows; 8 older.

    # First regeneration.
    first = await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=summariser,
        recent_window=2,
        trigger_delta=1,
    )
    assert first is not None
    assert len(summariser.calls) == 1
    assert len(summariser.calls[0]) == 8
    # No prior digest possible on first call.

    # Add more turns so the older slice grows.
    _seed_pairs(jarvis_store, 3)  # +6 rows; older slice now 14.

    # Second regeneration must see RAW older turns again (length 14).
    second = await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=summariser,
        recent_window=2,
        trigger_delta=1,
    )
    assert second is not None
    assert second.id != first.id
    assert len(summariser.calls) == 2
    second_input = summariser.calls[1]
    assert len(second_input) == 14

    # CRITICAL: the previous summary's TEXT must not appear in the input
    # to this regeneration. The pipeline must feed only RAW turns.
    prior_summary_text = first.text
    for entry in second_input:
        assert prior_summary_text not in entry.payload.get("content", "")

    # And: each input entry is a RAW user_turn / assistant_turn with the
    # original short content.
    contents = [e.payload["content"] for e in second_input]
    assert "u0" in contents
    assert "a0" in contents

    # Third regeneration after a small increment still sees RAW history.
    _seed_pairs(jarvis_store, 2)  # +4 rows; older slice now 18.
    third = await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=summariser,
        recent_window=2,
        trigger_delta=1,
    )
    assert third is not None
    assert len(summariser.calls) == 3
    third_input = summariser.calls[2]
    assert len(third_input) == 18
    for entry in third_input:
        for prior_text in (first.text, second.text):
            assert prior_text not in entry.payload.get("content", "")


@pytest.mark.asyncio
async def test_pipeline_skips_regeneration_when_threshold_not_met() -> None:
    """Two consecutive calls with no growth produce one summary, not two."""

    jarvis_store, summary_store = _setup()
    summariser = _RecordingSummariser()

    _seed_pairs(jarvis_store, 6)  # 12 rows; 8 older with K=2.

    first = await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=summariser,
        recent_window=2,
        trigger_delta=2,
    )
    assert first is not None
    assert len(summariser.calls) == 1

    # No new turns → threshold not met → store unchanged.
    second = await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=summariser,
        recent_window=2,
        trigger_delta=2,
    )
    assert second is not None
    assert second.id == first.id
    assert len(summariser.calls) == 1  # No new call.


@pytest.mark.asyncio
async def test_pipeline_persists_version_and_range() -> None:
    jarvis_store, summary_store = _setup()
    summariser = _RecordingSummariser()
    _seed_pairs(jarvis_store, 5)  # 10 rows; 6 older with K=2.

    result = await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=summariser,
        recent_window=2,
        trigger_delta=1,
    )
    assert result is not None
    assert result.summariser_version == SUMMARISER_VERSION
    # 6 older rows → to_turn = 6, from_turn = 1.
    assert result.from_turn == 1
    assert result.to_turn == 6
    assert summary_store.count() == 1


@pytest.mark.asyncio
async def test_pipeline_returns_existing_when_nothing_to_summarise() -> None:
    jarvis_store, summary_store = _setup()
    summariser = _RecordingSummariser()

    # Only 2 rows → all fit in the recent window with K=2.
    jarvis_store.append("user", "u")
    jarvis_store.append("assistant", "a")

    result = await maybe_regenerate_rolling_summary(
        jarvis_store=jarvis_store,
        summary_store=summary_store,
        summariser=summariser,
        recent_window=2,
        trigger_delta=1,
    )
    assert result is None
    assert summariser.calls == []
