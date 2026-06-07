"""Golden snapshot for the STATE-aware bounded prompt (PRD 0006 / issue 0050).

Pins the assembled prompt under ``bounded_v2_policy`` with a live
running task in :class:`bob.task_store.TaskStore`. Tightens the
contract for future PRs: any change to the STATE block layout (line
order, field names, recency literal, eviction order) fails this test
until ``BOB_UPDATE_SNAPSHOTS=1`` is set, forcing a conscious review.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from bob.context.assembler import ContextAssembler
from bob.context.entry import ContextEntry
from bob.context.policy import bounded_v2_policy
from bob.context.provider import ContextProvider
from bob.context.providers.cross_epoch_digest import CrossEpochDigestProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.state_block import StateBlockProvider
from bob.context.providers.system_block import SystemBlockProvider
from bob.context.providers.thinker_state import ThinkerStateProvider
from bob.context.providers.user_message import UserMessageProvider
from bob.context.summariser import SUMMARISER_VERSION, RollingSummary
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.epoch.digest import CrossEpochDigestStore
from bob.jarvis_store import JarvisStore
from bob.live_transcript_state import LiveTranscriptState
from bob.rolling_summary_store import RollingSummaryStore
from bob.task_store import TaskStore

from ._harness.golden_prompt import (
    assert_matches_snapshot,
    load_transcript_fixture,
    seed_history,
)


class _StaticSummariser:
    async def summarise(
        self,
        *,
        older_turns: Sequence[ContextEntry],
        from_turn: int,
        to_turn: int,
    ) -> RollingSummary | None:
        return None


def _setup() -> tuple[JarvisStore, RollingSummaryStore, CrossEpochDigestStore, TaskStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return (
        JarvisStore(conn),
        RollingSummaryStore(conn),
        CrossEpochDigestStore(conn),
        TaskStore(conn),
    )


def _seed_state(task_store: TaskStore, *, title: str, goal: str) -> str:
    task_id = task_store.create_task(title=title, goal=goal)
    task_store.update_state(task_id, "running")
    return task_id


def test_golden_snapshot_bounded_v2_with_state() -> None:
    """Bounded v2 policy with a single live task → STATE block sits after system block."""

    transcript = load_transcript_fixture("bounded_v2_with_state")
    jarvis_store, summary_store, digest_store, task_store = _setup()
    seed_history(jarvis_store, transcript)

    active = transcript["active_tasks"][0]
    task_id = _seed_state(task_store, title=active["title"], goal=active["goal"])

    fixed_now = lambda: datetime(2026, 5, 25, 12, 0, 0)  # noqa: E731
    providers: list[ContextProvider] = [
        SystemBlockProvider(system_content=transcript["system_content"]),
        StateBlockProvider(
            task_store=task_store,
            current_user_turn=2,
            last_referenced_turn_by_task={task_id: 2},
            now=fixed_now,
        ),
        CrossEpochDigestProvider(store=digest_store),
        RollingSummaryProvider(store=summary_store),
        # Empty store → no-op; registered to satisfy the v2 ``thinker_state``
        # slot (PRD 0016 / issue 0102) without perturbing the golden snapshot.
        ThinkerStateProvider(live_state=LiveTranscriptState()),
        RecentTurnsProvider(jarvis_store=jarvis_store),
        UserMessageProvider(),
    ]
    assembler = ContextAssembler(providers=providers, policy=bounded_v2_policy())
    messages = assembler.assemble(user_message=transcript["pending_user_message"])

    # Strip dynamic task id so the snapshot is stable.
    for msg in messages:
        if "id=" in msg["content"]:
            msg["content"] = msg["content"].replace(task_id, "<TASK_ID>")

    # ``SUMMARISER_VERSION`` import asserted so future-proofing the
    # fixture against a summariser version bump remains a one-line
    # change.
    assert SUMMARISER_VERSION >= 1
    assert_matches_snapshot(messages, "bounded_v2_with_state")
