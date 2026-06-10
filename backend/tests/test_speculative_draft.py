"""Unit tests for :class:`bob.speculative_draft.SpeculativeDraft` (issue 0104).

Two layers, both deterministic + offline:

- the PURE commit gate (:meth:`SpeculativeDraft.commit_gate`) — prefix fast-path,
  similarity guard above/below the threshold, divergence ⇒ discard, no-draft /
  tool-turn ⇒ discard with the right reason;
- the background loop cadence + lifecycle (mirrors the Thinker loop): a partial
  pre-writes one draft (``draft_status`` drafting → ready), DEBOUNCE coalesces a
  burst, a tool turn produces NO draft, and ``stop`` cancels a parked pass.

The emitted voice events are read back from the debug ring buffer (the same sink
:func:`bob.event_bus_v2.emit_event` writes to).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bob import debug_log
from bob.config import Settings
from bob.speculative_draft import (
    SpeculativeDraft,
    _is_prefix_match,
    _token_overlap,
)


class _Clock:
    """A manual monotonic clock — advance it explicitly to drive the debounce."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeDraftClient:
    """A scriptable ``draft`` :class:`LLMClient` with a per-call gate.

    Each :meth:`chat` call records the user text and returns the next scripted
    reply (or a default echo). An optional :class:`asyncio.Event` gate parks a
    call in flight to exercise the ≤1-in-flight + cancellation paths.
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self._replies = list(replies or [])
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    def supports_guided_json(self) -> bool:
        return False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self.calls.append(user)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self._replies:
            return self._replies.pop(0)
        return f"reply to: {user}"

    async def complete(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def _settings(
    *,
    debounce_ms: int = 250,
    grace_ms: int = 50,
    grace_cap_ms: int = 250,
    similarity: float = 0.6,
) -> Settings:
    return Settings.model_construct(
        THINKER_DEBOUNCE_MS=debounce_ms,
        THINKER_CANCEL_GRACE_MS=grace_ms,
        THINKER_CANCEL_GRACE_CAP_MS=grace_cap_ms,
        DRAFT_COMMIT_SIMILARITY=similarity,
        STT_DEBUG_TEXT_MAX_CHARS=64,
    )


def _drafter(
    client: _FakeDraftClient,
    *,
    clock: _Clock | None = None,
    similarity: float = 0.6,
    is_tool_intent: Any = None,
) -> SpeculativeDraft:
    return SpeculativeDraft(
        client=client,  # type: ignore[arg-type]
        settings=_settings(similarity=similarity),
        session_id="s1",
        spawn=asyncio.create_task,
        is_tool_intent=is_tool_intent,
        clock=clock or _Clock(),
    )


def _draft_status_events() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "draft_status":
            out.append(ws_event)
    return out


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    debug_log.clear()


# --- pure helpers ------------------------------------------------------------


def test_prefix_match_is_order_insensitive_to_which_is_longer() -> None:
    # The final extends the partial (the common settle case) → prefix.
    assert _is_prefix_match("quel temps fait il", "quel temps fait il a paris")
    # And symmetrically when the partial is the longer of the two.
    assert _is_prefix_match("quel temps fait il a paris", "quel temps fait il")
    # Punctuation / case / whitespace are normalised away.
    assert _is_prefix_match("Quel  temps", "quel temps, fait-il ?")


def test_prefix_match_rejects_divergent_and_empty() -> None:
    assert not _is_prefix_match("reserve une table", "annule tout finalement")
    assert not _is_prefix_match("", "anything")
    assert not _is_prefix_match("anything", "")


def test_token_overlap_ratio() -> None:
    assert _token_overlap("a b c", "a b c") == pytest.approx(1.0)
    assert _token_overlap("a b c d", "a b") == pytest.approx(0.5)  # 2 inter / 4 union
    assert _token_overlap("a b", "c d") == pytest.approx(0.0)
    assert _token_overlap("", "a b") == pytest.approx(0.0)


# --- the commit gate (pure) --------------------------------------------------


async def test_commit_gate_prefix_fast_path_commits() -> None:
    client = _FakeDraftClient(replies=["Il fait beau."])
    drafter = _drafter(client)
    drafter.start("t1")
    await drafter.feed_partial("quel temps fait il")
    await drafter.join()

    # Final EXTENDS the partial the draft fired on → prefix fast-path.
    decision = drafter.commit_gate("quel temps fait il a paris")
    assert decision.committed
    assert decision.reason == "prefix"
    assert decision.text == "Il fait beau."


async def test_commit_gate_similarity_above_threshold_commits() -> None:
    client = _FakeDraftClient(replies=["ok"])
    # Threshold 0.5 so a token-set overlap of 0.5 commits via the similarity guard.
    drafter = _drafter(client, similarity=0.5)
    drafter.start("t1")
    await drafter.feed_partial("rappelle moi demain matin")
    await drafter.join()

    # Not a prefix (a word changed mid-phrase), but heavy token overlap:
    # {rappelle, moi, demain, matin} vs {rappelle, moi, ce, matin} → 3/5 = 0.6.
    decision = drafter.commit_gate("rappelle moi ce matin")
    assert decision.committed
    assert decision.reason == "similarity"
    assert decision.similarity == pytest.approx(0.6)


async def test_commit_gate_below_threshold_discards_on_divergence() -> None:
    client = _FakeDraftClient(replies=["Je réserve une table."])
    drafter = _drafter(client, similarity=0.6)
    drafter.start("t1")
    await drafter.feed_partial("reserve une table pour ce soir")
    await drafter.join()

    # The user diverged at end of phrase: low overlap + not a prefix → discard.
    decision = drafter.commit_gate("annule tout finalement laisse tomber")
    assert not decision.committed
    assert decision.reason == "divergence"
    assert decision.similarity is not None and decision.similarity < 0.6


async def test_commit_gate_no_draft_discards() -> None:
    # Never fed a partial → no draft was produced.
    drafter = _drafter(_FakeDraftClient())
    drafter.start("t1")
    decision = drafter.commit_gate("quel temps fait il")
    assert not decision.committed
    assert decision.reason == "no_draft"
    assert decision.text == ""


# --- tool-dispatch turn ⇒ no draft (cold) ------------------------------------


async def test_tool_turn_produces_no_draft() -> None:
    client = _FakeDraftClient(replies=["should not be used"])
    # Classify any partial containing "rappel" as a tool turn (a reminder spawns a
    # sub-task) → the drafter must NOT speculate a conversational reply.
    drafter = _drafter(client, is_tool_intent=lambda text: "rappel" in text.lower())
    drafter.start("t1")
    await drafter.feed_partial("mets un rappel pour demain")
    await drafter.join()

    # No inference ran, no draft held; the gate reports the tool-turn reason so
    # the loop runs COLD (the cold say-path can dispatch the tool).
    assert client.calls == []
    assert drafter.draft_text is None
    decision = drafter.commit_gate("mets un rappel pour demain")
    assert not decision.committed
    assert decision.reason == "tool_turn"


# --- loop cadence + lifecycle (mirrors the Thinker loop) ---------------------


async def test_partial_pre_writes_one_draft_with_status_events() -> None:
    client = _FakeDraftClient(replies=["Il fait beau."])
    drafter = _drafter(client)
    drafter.start("t1")
    await drafter.feed_partial("quel temps fait il")
    await drafter.join()

    assert client.calls == ["quel temps fait il"]
    assert drafter.draft_text == "Il fait beau."

    states = [e["state"] for e in _draft_status_events()]
    assert "drafting" in states
    assert "ready" in states


async def test_debounce_coalesces_a_burst_to_one_inference() -> None:
    clock = _Clock()
    client = _FakeDraftClient(replies=["a", "b"])
    drafter = _drafter(client, clock=clock)
    drafter.start("t1")

    # First partial fires immediately; subsequent partials WITHIN the debounce
    # window (250 ms) only update the latest text — no new inference.
    await drafter.feed_partial("quel")
    await drafter.join()
    clock.advance(0.05)
    await drafter.feed_partial("quel temps")
    clock.advance(0.05)
    await drafter.feed_partial("quel temps fait il")
    await drafter.join()

    assert client.calls == ["quel"]


async def test_stop_cancels_a_parked_pass_then_keeps_held_draft() -> None:
    client = _FakeDraftClient(replies=["first"])
    client.gate = asyncio.Event()  # park the first pass
    drafter = _drafter(client)
    drafter.start("t1")

    await drafter.feed_partial("quel temps")
    await asyncio.wait_for(client.started.wait(), timeout=1.0)
    assert drafter.inflight

    # Stop while parked: grace elapses (50 ms) → hard-kill. No reply landed, so no
    # draft is held — the gate runs COLD on this turn.
    await drafter.stop()
    assert not drafter.inflight
    assert drafter.draft_text is None
    # A stopped loop accepts no further work.
    await drafter.feed_partial("more")
    assert client.calls == ["quel temps"]


async def test_stop_grace_is_capped_by_setting() -> None:
    """The configured 2 s grace is CAPPED (PRD 0018 / issue 0118).

    A pass that would stall through the whole cooperative grace (the gate is
    never set) is hard-cancelled at ``THINKER_CANCEL_GRACE_CAP_MS`` instead:
    ``stop`` returns in the cap window, nowhere near the 2 s grace.
    """

    client = _FakeDraftClient()
    client.gate = asyncio.Event()  # never set — the pass would stall forever
    drafter = SpeculativeDraft(
        client=client,  # type: ignore[arg-type]
        settings=_settings(grace_ms=2_000, grace_cap_ms=30),
        session_id="s1",
        spawn=asyncio.create_task,
        clock=_Clock(),
    )
    drafter.start("t1")
    await drafter.feed_partial("bloque")
    await asyncio.wait_for(client.started.wait(), timeout=1.0)
    assert drafter.inflight

    started = asyncio.get_running_loop().time()
    await drafter.stop()  # the 30 ms cap elapses → hard cancel (not the 2 s grace)
    elapsed = asyncio.get_running_loop().time() - started

    assert not drafter.inflight
    assert elapsed < 1.0, f"stop took {elapsed:.3f}s — the grace cap did not apply"
    # The hard-cancelled pass landed no draft — the gate runs COLD on this turn.
    assert drafter.draft_text is None


async def test_hard_cancel_is_zero_grace_even_when_the_pass_stalls() -> None:
    """``hard_cancel`` (PRD 0018 / issue 0119) never awaits the in-flight pass.

    Mirrors the ThinkerLoop contract: the pass SWALLOWS the cooperative cancel
    (even the post-grace escalation of :meth:`stop` would hang in its final
    await), and the zero-grace ``hard_cancel`` is synchronous — it latches the
    stop flag, requests the hard ``Task.cancel`` and returns while the stubborn
    task is STILL parked.
    """

    client = _FakeDraftClient()
    escape = asyncio.Event()

    async def _stubborn_chat(
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        client.started.set()
        while not escape.is_set():
            try:
                await escape.wait()
            except asyncio.CancelledError:
                continue  # the cooperative cancel stalls — by design
        return "late reply"

    client.chat = _stubborn_chat  # type: ignore[method-assign]
    tasks: list[asyncio.Task[None]] = []

    def _spawn(coro: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        tasks.append(task)
        return task

    drafter = SpeculativeDraft(
        client=client,  # type: ignore[arg-type]
        settings=_settings(grace_ms=2_000, grace_cap_ms=250),
        session_id="s1",
        spawn=_spawn,
        clock=_Clock(),
    )
    drafter.start("t1")
    await drafter.feed_partial("bloque")
    await asyncio.wait_for(client.started.wait(), timeout=1.0)
    assert drafter.inflight is True

    drafter.hard_cancel()  # synchronous — returns without ANY grace or await

    assert drafter.inflight is False
    # The stubborn task is still parked: the cut never waited on its unwind.
    assert tasks and all(not task.done() for task in tasks)
    # The loop is latched stopped — further partials are dropped; no draft held.
    await drafter.feed_partial("après hard_cancel")
    assert drafter.inflight is False
    assert drafter.draft_text is None

    escape.set()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_hard_cancel_keeps_the_already_landed_draft() -> None:
    """``hard_cancel`` shares :meth:`stop`'s draft contract: the held draft
    survives the cut (the next :meth:`start` clears it — on a barge-in the
    loop re-arms immediately, dropping the stale speculation)."""

    client = _FakeDraftClient(replies=["déjà posé"])
    drafter = _drafter(client)
    drafter.start("t1")
    await drafter.feed_partial("bonjour")
    await drafter.join()
    assert drafter.draft_text == "déjà posé"

    drafter.hard_cancel()
    assert drafter.draft_text == "déjà posé"
    # The barge-in re-arm then drops the stale speculation.
    drafter.start("t1")
    assert drafter.draft_text is None


async def test_start_clears_previous_turn_draft() -> None:
    client = _FakeDraftClient(replies=["turn-1 reply"])
    drafter = _drafter(client)
    drafter.start("t1")
    await drafter.feed_partial("bonjour")
    await drafter.join()
    assert drafter.draft_text == "turn-1 reply"

    # Re-arming for a new turn drops the stale pre-written reply.
    drafter.start("t2")
    assert drafter.draft_text is None
    decision = drafter.commit_gate("bonjour")
    assert decision.reason == "no_draft"


# --- lifecycle epoch (stale-pass guard) ---------------------------------------


async def test_stale_generation_pass_never_stores_a_draft() -> None:
    """A pass that outlives a hard_cancel + re-arm (same turn id) drops its reply.

    ``hard_cancel`` never awaits the cancelled task, so a pass past its last
    await point can conclude AFTER a barge-in resume re-armed the SAME turn id
    (the ``turn_id`` compare does not discriminate there). The lifecycle epoch
    captured at launch must reject the stale store — otherwise the resumed turn
    could adopt a draft written for speech that no longer matches.
    """

    client = _FakeDraftClient(replies=["stale reply", "fresh reply"])
    drafter = _drafter(client)

    drafter.start("t1")
    # Park a pass IN FLIGHT inside the model call — the dangerous window.
    client.gate = asyncio.Event()
    stale_pass = asyncio.create_task(drafter._run_pass("salut bob", drafter._generation))
    await client.started.wait()

    # Barge-in: zero-grace cancel (the task is never awaited) + resume re-arms
    # the SAME turn id.
    drafter.hard_cancel()
    drafter.start("t1")

    # The surviving pass resumes past its await and concludes against the
    # re-armed turn — the epoch guard must drop its reply.
    client.gate.set()
    await stale_pass
    assert drafter.draft_text is None

    # A pass of the CURRENT epoch stores normally.
    client.gate = None
    await drafter._run_pass("salut bob", drafter._generation)
    assert drafter.draft_text == "fresh reply"


async def test_stale_generation_pass_never_reschedules_a_rerun() -> None:
    """A stale pass with a pending rerun must not relaunch into the new epoch."""

    client = _FakeDraftClient()
    drafter = _drafter(client)

    drafter.start("t1")
    stale_generation = drafter._generation
    drafter.hard_cancel()
    drafter.start("t1")
    drafter._rerun = True
    drafter._pending_text = "vieux partiel"

    await drafter._maybe_rerun(stale_generation)
    assert drafter.inflight is False
    assert client.calls == []
