"""Long-session smoke test for the bounded :class:`ContextPolicy`.

PRD 0006 / issue 0046 acceptance criterion: synthetic 200-turn conversation
shows assembled-prompt token count plateaus past turn ~30 (within ±10 %).
The user-visible payoff of issue 0046 is exactly this — bounded context
means session length stops slowing Jarvis down.

We drive the assembler end-to-end with the bounded providers + a
deterministic :class:`FixedTextSummariser`. After each user turn we run
the summary-regeneration pipeline, assemble the prompt and record its
token count via :class:`WordCountTokenizer`. Assertions on the recorded
series:

1. After turn ~30, every subsequent token count is within ±10 % of the
   running median past that point. ("Plateau" — not "exact constant", to
   allow the rolling summary to grow modestly under the deterministic
   fake summariser.)
2. The series past turn 50 stays under 2x the legacy "send the whole
   thread" baseline at turn 50 (i.e. bounded grows slower than legacy
   over time). We check this loosely.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import pytest

from bob.context.assembler import ContextAssembler
from bob.context.entry import ContextEntry
from bob.context.policy import bounded_v1_policy
from bob.context.providers.legacy_full_history import LegacyFullHistoryProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.system_block import SystemBlockProvider
from bob.context.providers.thinker_state import ThinkerStateProvider
from bob.context.providers.user_message import UserMessageProvider
from bob.context.summariser import SUMMARISER_VERSION, RollingSummary
from bob.context.summary_pipeline import maybe_regenerate_rolling_summary
from bob.context.tokenizer import WordCountTokenizer
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.live_transcript_state import LiveTranscriptState
from bob.rolling_summary_store import RollingSummaryStore


class _CappedSummariser:
    """Deterministic summariser whose output is bounded in tokens.

    Simulates the user-visible property of a real LLM summariser: the
    digest size is bounded regardless of input transcript length. The
    bounded ``ContextPolicy`` relies on this contract — without it the
    rolling summary block grows linearly and the whole point of the
    policy is defeated.

    The summariser:

    * reads the RAW older turns each call (no state),
    * stamps the canonical :data:`SUMMARISER_VERSION`,
    * emits a fixed-length token signature instead of the raw transcript.
    """

    def __init__(self, *, max_words: int = 30) -> None:
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
        # Build a capped digest from the FIRST few + LAST few raw turns.
        # The transcript is intentionally not echoed verbatim — the cap
        # keeps the produced summary token-bounded.
        head = " ".join(f"u{i}" for i in range(min(3, len(older_turns))))
        tail = " ".join(f"u{i}" for i in range(max(0, len(older_turns) - 3), len(older_turns)))
        digest_body = f"digest {head} ... {tail}"
        words = digest_body.split()[: self._max_words]
        text = " ".join(words)
        return RollingSummary(
            text=text,
            summariser_version=SUMMARISER_VERSION,
            from_turn=from_turn,
            to_turn=to_turn,
            raw_turn_count=len(older_turns),
        )


_SYSTEM_PROMPT = (
    "Tu es Jarvis. Réponds en français de manière concise. "
    "Tu disposes de plusieurs outils mais les détails sont gérés par l'orchestrator."
)

_TOTAL_TURNS = 200
_RECENT_WINDOW = 3


def _make_stores() -> tuple[JarvisStore, RollingSummaryStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return JarvisStore(conn), RollingSummaryStore(conn)


def _assemble_bounded(
    *,
    jarvis_store: JarvisStore,
    summary_store: RollingSummaryStore,
    system_content: str,
    user_message: str,
) -> list[dict[str, str]]:
    """Compose the bounded prompt for the live user message."""

    from bob.context.provider import ContextProvider

    policy = bounded_v1_policy()
    providers: list[ContextProvider] = [
        SystemBlockProvider(system_content=system_content),
        RollingSummaryProvider(store=summary_store),
        # Empty store → no-op; satisfies the bounded ``thinker_state`` slot
        # (PRD 0016 / issue 0102) without changing the plateau behaviour.
        ThinkerStateProvider(live_state=LiveTranscriptState()),
        RecentTurnsProvider(jarvis_store=jarvis_store),
        UserMessageProvider(),
    ]
    assembler = ContextAssembler(providers=providers, policy=policy)
    return assembler.assemble(user_message=user_message)


def _assemble_legacy(*, jarvis_store: JarvisStore, system_content: str) -> list[dict[str, str]]:
    from bob.context.policy import legacy_full_history_policy

    provider = LegacyFullHistoryProvider(jarvis_store=jarvis_store, system_content=system_content)
    assembler = ContextAssembler(providers=[provider], policy=legacy_full_history_policy())
    return assembler.assemble()


def _total_tokens(messages: list[dict[str, str]], tokenizer: WordCountTokenizer) -> int:
    return sum(tokenizer.count(m["content"]) for m in messages)


@pytest.mark.asyncio
async def test_bounded_policy_plateaus_over_long_session() -> None:
    """Drive 200 synthetic turns; assert token count plateaus past turn 30."""

    jarvis_store, summary_store = _make_stores()
    summariser = _CappedSummariser(max_words=30)
    tokenizer = WordCountTokenizer()

    bounded_series: list[int] = []
    legacy_series: list[int] = []

    for turn_idx in range(_TOTAL_TURNS):
        user_text = (
            f"Turn {turn_idx}: peux-tu m'expliquer le sujet number {turn_idx} en "
            f"détail s'il te plaît ?"
        )
        # Orchestrator persists the user turn first.
        jarvis_store.append("user", user_text)

        # Run the summary pipeline (bounded path).
        await maybe_regenerate_rolling_summary(
            jarvis_store=jarvis_store,
            summary_store=summary_store,
            summariser=summariser,
            recent_window=_RECENT_WINDOW,
            trigger_delta=2,
        )

        bounded_messages = _assemble_bounded(
            jarvis_store=jarvis_store,
            summary_store=summary_store,
            system_content=_SYSTEM_PROMPT,
            user_message=user_text,
        )
        bounded_series.append(_total_tokens(bounded_messages, tokenizer))

        legacy_messages = _assemble_legacy(jarvis_store=jarvis_store, system_content=_SYSTEM_PROMPT)
        legacy_series.append(_total_tokens(legacy_messages, tokenizer))

        # Simulate the assistant reply for the next turn.
        jarvis_store.append("assistant", f"Réponse {turn_idx}: voici l'explication courte.")

    assert len(bounded_series) == _TOTAL_TURNS

    # --- Plateau assertion ----------------------------------------------------
    # Past turn 30 the bounded series should be roughly stationary. The
    # capped summariser emulates the real LLM contract (bounded digest
    # regardless of input transcript length), so the prompt size is the
    # sum of:
    #   - system block (constant),
    #   - rolling summary block (capped at ~30 words by the summariser),
    #   - recent window (2*K rows, ≤K from policy = constant),
    #   - live user message (varies in [O(1) words]).
    # All four terms are bounded. We assert every value past turn 30 is
    # within ±10 % of the median past that point — the PRD's acceptance
    # criterion verbatim.
    tail = bounded_series[30:]
    tail_median = sorted(tail)[len(tail) // 2]
    lower = tail_median * 0.9
    upper = tail_median * 1.1
    out_of_band = [(i, v) for i, v in enumerate(tail, start=30) if v < lower or v > upper]
    assert not out_of_band, (
        f"bounded series should plateau past turn 30 (median={tail_median}); "
        f"out-of-band values: {out_of_band[:10]}"
    )

    # --- Legacy comparator ----------------------------------------------------
    # Legacy series grows linearly with turn count. Past turn 100 it must
    # be at least 2x the bounded series — sanity check that the bounded
    # policy actually saved tokens over the long run.
    assert legacy_series[100] >= 2 * bounded_series[100], (
        f"bounded should be materially smaller than legacy past turn 100: "
        f"bounded={bounded_series[100]} legacy={legacy_series[100]}"
    )
    assert legacy_series[-1] >= 5 * bounded_series[-1], (
        "by the end of the session the legacy prompt must dwarf the bounded one: "
        f"bounded[-1]={bounded_series[-1]} legacy[-1]={legacy_series[-1]}"
    )
