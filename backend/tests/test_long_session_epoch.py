"""Long-session synthetic test for the epoch sealer (PRD 0006 / issue 0051).

Drives a 500-turn conversation through the full bounded-v2 stack
(:class:`SystemBlockProvider` + :class:`CrossEpochDigestProvider` +
:class:`RollingSummaryProvider` + :class:`RecentTurnsProvider` +
:class:`UserMessageProvider`) plus the :class:`EpochManager` token-
threshold sealer. Asserts:

1. ≥ 3 seals fire over the session.
2. Assembled-prompt token count stays bounded past turn 30 (the
   bounded plateau established in issue 0046 — must hold under epoch
   sealing).
3. Cross-epoch digest length stays within
   :attr:`EpochPolicy.max_digest_size`.

We use a capped fake summariser (mirrors the issue 0046 long-session
test) so the rolling summary's token count grows predictably and
crosses the configured threshold ≥ 3 times across 500 turns.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import pytest

from bob.context.assembler import ContextAssembler
from bob.context.entry import ContextEntry
from bob.context.policy import bounded_v2_policy
from bob.context.providers.cross_epoch_digest import CrossEpochDigestProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.system_block import SystemBlockProvider
from bob.context.providers.user_message import UserMessageProvider
from bob.context.summariser import SUMMARISER_VERSION, RollingSummary
from bob.context.summary_pipeline import maybe_regenerate_rolling_summary
from bob.context.tokenizer import WordCountTokenizer
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.epoch.digest import CrossEpochDigestStore
from bob.epoch.manager import EpochManager
from bob.epoch.policy import EpochPolicy
from bob.jarvis_store import JarvisStore
from bob.rolling_summary_store import RollingSummaryStore

_SYSTEM_PROMPT = "Tu es Jarvis. Réponds concis."
_RECENT_WINDOW = 3
_TOTAL_TURNS = 500
_TOKEN_THRESHOLD = 20  # Low — forces ≥3 seals over 500 turns.
_MAX_DIGEST_SIZE = 1200


class _GrowingSummariser:
    """Capped digest summariser whose output crosses the token threshold cleanly.

    Issue 0046's capped fake produces a fixed-length signature; here
    we let the digest grow modestly with the input transcript length
    (clipped at ``max_words``) so the per-epoch rolling summary
    actually crosses the configured token threshold and triggers
    sealing.
    """

    def __init__(self, *, max_words: int = 50) -> None:
        self._max_words = max_words

    async def summarise(
        self,
        *,
        older_turns: Sequence[ContextEntry],
        from_turn: int,
        to_turn: int,
    ) -> RollingSummary | None:
        if not older_turns:
            return None
        words = (
            " ".join(f"digest{i:03d}" for i in range(min(len(older_turns), self._max_words)))
        ).split()[: self._max_words]
        text = " ".join(words)
        return RollingSummary(
            text=text,
            summariser_version=SUMMARISER_VERSION,
            from_turn=from_turn,
            to_turn=to_turn,
            raw_turn_count=len(older_turns),
        )


def _setup() -> tuple[sqlite3.Connection, JarvisStore, RollingSummaryStore, CrossEpochDigestStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return conn, JarvisStore(conn), RollingSummaryStore(conn), CrossEpochDigestStore(conn)


def _assemble(
    *,
    jarvis_store: JarvisStore,
    summary_store: RollingSummaryStore,
    digest_store: CrossEpochDigestStore,
    user_message: str,
) -> list[dict[str, str]]:
    """Compose the bounded-v2 prompt for the live user message."""

    from bob.context.provider import ContextProvider
    from bob.context.providers.state_block import StateBlockProvider
    from bob.context.providers.thinker_state import ThinkerStateProvider
    from bob.live_transcript_state import LiveTranscriptState
    from bob.task_store import TaskStore

    # The long-session test never spawns tasks nor speaks; we still register the
    # ``state_block`` + ``thinker_state`` providers so the v2 policy provider
    # list resolves. Both read empty stores → no-op, so the prompt-bound series
    # is unchanged (PRD 0006 / issue 0050; PRD 0016 / issue 0102).
    task_store = TaskStore(jarvis_store._conn)
    providers: list[ContextProvider] = [
        SystemBlockProvider(system_content=_SYSTEM_PROMPT),
        StateBlockProvider(task_store=task_store),
        CrossEpochDigestProvider(store=digest_store),
        RollingSummaryProvider(store=summary_store),
        ThinkerStateProvider(live_state=LiveTranscriptState()),
        RecentTurnsProvider(jarvis_store=jarvis_store),
        UserMessageProvider(),
    ]
    assembler = ContextAssembler(providers=providers, policy=bounded_v2_policy())
    return assembler.assemble(user_message=user_message)


def _total_tokens(messages: list[dict[str, str]], tokenizer: WordCountTokenizer) -> int:
    return sum(tokenizer.count(m["content"]) for m in messages)


@pytest.mark.asyncio
async def test_long_session_triggers_seals_and_keeps_prompt_bounded() -> None:
    """500 turns, low threshold → ≥ 3 seals, prompt bounded, digest capped."""

    conn, js, rolling, digests = _setup()
    summariser = _GrowingSummariser(max_words=50)
    tokenizer = WordCountTokenizer()
    policy = EpochPolicy(token_threshold=_TOKEN_THRESHOLD, max_digest_size=_MAX_DIGEST_SIZE)
    manager = EpochManager(
        policy=policy,
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )

    bounded_series: list[int] = []
    digest_sizes: list[int] = []

    for turn_idx in range(_TOTAL_TURNS):
        user_text = f"Turn {turn_idx}: explique le sujet number {turn_idx} en détail s'il te plaît"
        js.append("user", user_text)

        # Rolling summary regeneration + seal lifecycle. The pipeline
        # stamps each new summary with the live epoch id so the manager
        # can distinguish current-epoch summaries from sealed ones.
        await maybe_regenerate_rolling_summary(
            jarvis_store=js,
            summary_store=rolling,
            summariser=summariser,
            recent_window=_RECENT_WINDOW,
            trigger_delta=2,
            current_epoch_id=manager.current_epoch_id,
        )
        manager.apply_seal()

        messages = _assemble(
            jarvis_store=js,
            summary_store=rolling,
            digest_store=digests,
            user_message=user_text,
        )
        bounded_series.append(_total_tokens(messages, tokenizer))

        latest_digest = digests.latest()
        digest_sizes.append(len(latest_digest.text) if latest_digest else 0)

        js.append("assistant", f"Réponse {turn_idx}: voici l'explication courte.")

    # --- Acceptance criterion 1: ≥ 3 seals fired.
    assert manager.current_epoch_id >= 3, (
        f"expected ≥ 3 seals, got current_epoch_id={manager.current_epoch_id}"
    )

    # --- Acceptance criterion 2: prompt size bounded past turn 30.
    # The plateau bound is laxer than issue 0046 (±10%) because epoch
    # sealing introduces step-changes when the digest grows; we assert
    # a hard upper bound instead.
    tail = bounded_series[30:]
    plateau_max = max(tail)
    # An upper bound large enough to accommodate the digest growth but
    # tight enough to flag pathological linear growth.
    upper_bound = 600  # words; well below legacy ~5k+ at turn 500.
    assert plateau_max < upper_bound, (
        f"bounded-v2 prompt should stay under {upper_bound} tokens, got max={plateau_max}"
    )

    # --- Acceptance criterion 3: cross-epoch digest stays under cap.
    final_digest = digests.latest()
    assert final_digest is not None
    assert len(final_digest.text) <= _MAX_DIGEST_SIZE, (
        f"digest exceeds cap: len={len(final_digest.text)} cap={_MAX_DIGEST_SIZE}"
    )
    # And no per-turn snapshot exceeded the cap either.
    assert max(digest_sizes) <= _MAX_DIGEST_SIZE
