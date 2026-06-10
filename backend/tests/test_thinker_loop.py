"""Unit tests for :class:`bob.thinker_loop.ThinkerLoop` (PRD 0016 / issue 0102).

Drives the loop with a controllable fake ``thinker`` client + an injected clock
(deterministic debounce) and asserts the Annexe H cadence + lifecycle:

- a partial → one inference → snapshot in the store + a ``thinker_snapshot``
  voice event (``user_turn_complete`` present in the payload);
- DEBOUNCE: rapid partials within the window coalesce to ONE inference;
- ≤1 inference in flight: a partial arriving mid-pass triggers exactly ONE rerun
  against the newest text;
- cooperative cancellation: ``stop`` cancels a parked in-flight pass (grace then
  hard-kill), and a stopped loop accepts no further work.

The emitted voice events are read back from the debug ring buffer (the same sink
:func:`bob.event_bus_v2.emit_event` writes to).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from bob import debug_log
from bob.config import Settings
from bob.live_transcript_state import LiveTranscriptState
from bob.thinker_loop import ThinkerLoop, _parse_snapshot_json


class _Clock:
    """A manual monotonic clock — advance it explicitly to drive the debounce."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeThinkerClient:
    """A scriptable ``thinker`` :class:`LLMClient` with a per-call gate.

    Each :meth:`chat` call records the prompt and returns the next scripted JSON
    reply. An optional :class:`asyncio.Event` gate lets a test PARK a call in
    flight (to exercise the ≤1-in-flight + cancellation paths): the call awaits
    the gate before returning.
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
        return json.dumps({"corrected_text": user, "next_step_plan": "plan"})

    async def complete(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def _settings(*, debounce_ms: int = 250, grace_ms: int = 50, grace_cap_ms: int = 250) -> Settings:
    return Settings.model_construct(
        THINKER_DEBOUNCE_MS=debounce_ms,
        THINKER_CANCEL_GRACE_MS=grace_ms,
        THINKER_CANCEL_GRACE_CAP_MS=grace_cap_ms,
        STT_DEBUG_TEXT_MAX_CHARS=64,
    )


def _loop(
    client: _FakeThinkerClient,
    state: LiveTranscriptState,
    clock: _Clock,
    *,
    debounce_ms: int = 250,
    grace_ms: int = 50,
    grace_cap_ms: int = 250,
) -> ThinkerLoop:
    return ThinkerLoop(
        client=client,  # type: ignore[arg-type]
        live_state=state,
        settings=_settings(debounce_ms=debounce_ms, grace_ms=grace_ms, grace_cap_ms=grace_cap_ms),
        session_id="s1",
        clock=clock,
    )


def _snapshots() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in debug_log.snapshot():
        ws_event = (event.payload or {}).get("ws_event") or {}
        if ws_event.get("type") == "thinker_snapshot":
            out.append(ws_event)
    return out


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    debug_log.clear()


# --- happy path --------------------------------------------------------------


async def test_partial_produces_snapshot_and_event() -> None:
    client = _FakeThinkerClient(
        replies=[
            json.dumps(
                {
                    "corrected_text": "quel temps à Paris",
                    "variables": {"city": "Paris"},
                    "next_step_plan": "donner la météo",
                    "user_turn_complete": True,
                    "backchannel": "mm",
                }
            )
        ]
    )
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock)
    loop.start("t1")

    await loop.feed_partial("quel temps")
    await loop.join()

    snap = state.latest()
    assert snap is not None
    assert snap.turn_id == "t1"
    assert snap.seq == 1
    assert snap.corrected_text == "quel temps à Paris"
    assert snap.variables == {"city": "Paris"}
    assert snap.user_turn_complete is True
    assert snap.backchannel == "mm"

    events = _snapshots()
    assert len(events) == 1
    payload = events[0]
    assert payload["turn_id"] == "t1"
    assert payload["seq"] == 1
    # ``user_turn_complete`` is present in the payload (carried; consumed in 0103).
    assert payload["user_turn_complete"] is True


async def test_seq_increments_across_passes() -> None:
    client = _FakeThinkerClient()
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock, debounce_ms=100)

    loop.start("t1")
    await loop.feed_partial("un")
    await loop.join()
    clock.advance(0.2)  # past the debounce window
    await loop.feed_partial("un deux")
    await loop.join()

    assert client.calls == ["un", "un deux"]
    latest = state.latest()
    assert latest is not None
    assert latest.seq == 2


# --- debounce (Annexe H) -----------------------------------------------------


async def test_debounce_coalesces_rapid_partials() -> None:
    """Two partials inside the debounce window fire only ONE inference."""

    client = _FakeThinkerClient()
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock, debounce_ms=250)

    loop.start("t1")
    await loop.feed_partial("quel")
    await loop.join()
    # Second partial 100 ms later — inside the 250 ms window → coalesced (no call).
    clock.advance(0.1)
    await loop.feed_partial("quel temps")
    await loop.join()

    assert client.calls == ["quel"]


async def test_debounce_window_elapsed_fires_again() -> None:
    client = _FakeThinkerClient()
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock, debounce_ms=250)

    loop.start("t1")
    await loop.feed_partial("quel")
    await loop.join()
    clock.advance(0.3)  # past the window
    await loop.feed_partial("quel temps fait-il")
    await loop.join()

    assert client.calls == ["quel", "quel temps fait-il"]


# --- semantic-endpoint fast path (PRD 0018 / issue 0120) ----------------------


def _complete_reply(text: str, *, complete: bool) -> str:
    return json.dumps({"corrected_text": text, "user_turn_complete": complete})


async def test_turn_complete_bit_pushed_at_pass_conclusion() -> None:
    """The bit reaches the endpoint hook the instant the pass concludes.

    Timestamps under the fake clock: the push lands at the trigger instant (the
    clock never advanced during the pass), i.e. strictly INSIDE the still-open
    250 ms debounce window — the signal did not wait for the window.
    """

    client = _FakeThinkerClient(replies=[_complete_reply("c'est fini", complete=True)])
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock, debounce_ms=250)
    received: list[tuple[float, bool]] = []
    loop.on_turn_complete = lambda complete: received.append((clock.now, complete))

    loop.start("t1")
    trigger_ts = clock.now
    await loop.feed_partial("c'est fini")
    await loop.join()

    # Delivered exactly once, at the pass-conclusion timestamp — well before
    # the debounce window (trigger_ts + 0.250) would have elapsed.
    assert received == [(trigger_ts, True)]


async def test_turn_complete_push_keeps_pass_debounce_for_rest_of_payload() -> None:
    """Only the bit escapes the debounce — the NEXT inference stays debounced."""

    client = _FakeThinkerClient(replies=[_complete_reply("fin", complete=True)])
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock, debounce_ms=250)
    received: list[bool] = []
    loop.on_turn_complete = received.append

    loop.start("t1")
    await loop.feed_partial("fin")
    await loop.join()
    assert received == [True]

    # A partial INSIDE the window coalesces exactly as before (no second model
    # call, hence no second push) even though the bit already propagated.
    clock.advance(0.1)
    await loop.feed_partial("fin du tour")
    await loop.join()
    assert client.calls == ["fin"]
    assert received == [True]


async def test_turn_complete_withdrawal_pushed_immediately_too() -> None:
    """A later pass that withdraws the bit pushes ``False`` at its conclusion."""

    client = _FakeThinkerClient(
        replies=[
            _complete_reply("fin", complete=True),
            _complete_reply("fin mais en fait", complete=False),
        ]
    )
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock, debounce_ms=250)
    received: list[bool] = []
    loop.on_turn_complete = received.append

    loop.start("t1")
    await loop.feed_partial("fin")
    await loop.join()
    clock.advance(0.3)  # past the window — a fresh pass is accepted
    await loop.feed_partial("fin mais en fait")
    await loop.join()

    assert received == [True, False]


async def test_turn_complete_push_failure_keeps_snapshot_and_event() -> None:
    """A failing hook is logged and dropped — the pass still lands its snapshot."""

    client = _FakeThinkerClient(replies=[_complete_reply("fin", complete=True)])
    state = LiveTranscriptState()
    loop = _loop(client, state, _Clock())

    def _boom(complete: bool) -> None:
        raise RuntimeError("hook down")

    loop.on_turn_complete = _boom
    loop.start("t1")
    await loop.feed_partial("fin")
    await loop.join()

    snap = state.latest()
    assert snap is not None
    assert snap.user_turn_complete is True
    assert len(_snapshots()) == 1


# --- ≤1 inference in flight (Annexe H) ---------------------------------------


async def test_single_inference_in_flight_then_one_rerun() -> None:
    """A partial arriving mid-pass triggers exactly ONE rerun on the newest text."""

    client = _FakeThinkerClient()
    client.gate = asyncio.Event()  # park the first pass in flight
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock)

    loop.start("t1")
    await loop.feed_partial("première")
    await client.started.wait()  # the first inference is now parked on the gate
    assert loop.inflight is True

    # Two more partials WHILE the first pass is parked — must NOT spawn a second
    # inference (≤1 in flight); they update the pending text + flag one rerun.
    await loop.feed_partial("première deux")
    await loop.feed_partial("première deux trois")
    assert client.calls == ["première"]  # still just the one in flight

    # Release the gate: the first pass completes, then exactly one rerun fires
    # against the NEWEST pending text.
    client.started.clear()
    client.gate.set()
    await loop.join()
    await client.started.wait()
    await loop.join()

    assert client.calls == ["première", "première deux trois"]
    # No further rerun after the queued one drains.
    await asyncio.sleep(0)
    assert client.calls == ["première", "première deux trois"]


# --- cooperative cancellation (Annexe H) -------------------------------------


async def test_stop_cancels_inflight_pass() -> None:
    """``stop`` hard-kills a parked in-flight inference after the grace window."""

    client = _FakeThinkerClient()
    client.gate = asyncio.Event()  # never set — the pass blocks forever
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock, grace_ms=20)

    loop.start("t1")
    await loop.feed_partial("bloque")
    await client.started.wait()
    assert loop.inflight is True

    await loop.stop()  # grace elapses (gate never set) → hard cancel
    assert loop.inflight is False
    # The cancelled pass produced no snapshot / event.
    assert state.latest() is None
    assert _snapshots() == []


async def test_stop_grace_is_capped_by_setting() -> None:
    """The configured 2 s grace is CAPPED (PRD 0018 / issue 0118).

    A pass that would stall through the whole cooperative grace (the gate is
    never set) is hard-cancelled at ``THINKER_CANCEL_GRACE_CAP_MS`` instead:
    ``stop`` returns in the cap window, nowhere near the 2 s grace.
    """

    client = _FakeThinkerClient()
    client.gate = asyncio.Event()  # never set — the pass would stall forever
    state = LiveTranscriptState()
    loop = _loop(client, state, _Clock(), grace_ms=2_000, grace_cap_ms=30)

    loop.start("t1")
    await loop.feed_partial("bloque")
    await client.started.wait()
    assert loop.inflight is True

    started = asyncio.get_running_loop().time()
    await loop.stop()  # the 30 ms cap elapses → hard cancel (not the 2 s grace)
    elapsed = asyncio.get_running_loop().time() - started

    assert loop.inflight is False
    assert elapsed < 1.0, f"stop took {elapsed:.3f}s — the grace cap did not apply"
    # The hard-cancelled pass produced no snapshot / event.
    assert state.latest() is None
    assert _snapshots() == []


async def test_hard_cancel_is_zero_grace_even_when_the_pass_stalls() -> None:
    """``hard_cancel`` (PRD 0018 / issue 0119) never awaits the in-flight pass.

    The pass here SWALLOWS the cooperative cancel (worse than parked: even the
    post-grace escalation of :meth:`stop` would hang in its final await). The
    zero-grace ``hard_cancel`` is synchronous: it latches the stop flag,
    requests the hard ``Task.cancel`` and returns — the loop reports idle while
    the stubborn task is STILL parked.
    """

    client = _FakeThinkerClient()
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
        return "{}"

    client.chat = _stubborn_chat  # type: ignore[method-assign]
    tasks: list[asyncio.Task[None]] = []

    def _spawn(coro: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        tasks.append(task)
        return task

    state = LiveTranscriptState()
    loop = ThinkerLoop(
        client=client,  # type: ignore[arg-type]
        live_state=state,
        settings=_settings(grace_ms=2_000, grace_cap_ms=250),
        session_id="s1",
        spawn=_spawn,
        clock=_Clock(),
    )
    loop.start("t1")
    await loop.feed_partial("bloque")
    await asyncio.wait_for(client.started.wait(), timeout=1.0)
    assert loop.inflight is True

    loop.hard_cancel()  # synchronous — returns without ANY grace or await

    assert loop.inflight is False
    # The stubborn task is still parked: the cut never waited on its unwind.
    assert tasks and all(not task.done() for task in tasks)
    # The loop is latched stopped — further partials are dropped.
    await loop.feed_partial("après hard_cancel")
    assert loop.inflight is False
    # No snapshot leaked from the cancelled pass.
    assert state.latest() is None

    escape.set()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_hard_cancel_without_inflight_pass_is_a_noop() -> None:
    client = _FakeThinkerClient()
    loop = _loop(client, LiveTranscriptState(), _Clock())
    loop.hard_cancel()  # nothing armed / in flight — silent no-op
    loop.start("t1")
    loop.hard_cancel()
    loop.hard_cancel()  # idempotent
    await loop.feed_partial("après")
    assert client.calls == []


async def test_stopped_loop_ignores_further_partials() -> None:
    client = _FakeThinkerClient()
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock)

    loop.start("t1")
    await loop.stop()
    await loop.feed_partial("après stop")
    await loop.join()
    assert client.calls == []


async def test_feed_before_start_is_noop() -> None:
    client = _FakeThinkerClient()
    state = LiveTranscriptState()
    loop = _loop(client, state, _Clock())
    await loop.feed_partial("pas armé")
    await loop.join()
    assert client.calls == []


async def test_start_clears_previous_turn_store() -> None:
    client = _FakeThinkerClient()
    state = LiveTranscriptState()
    clock = _Clock()
    loop = _loop(client, state, clock)

    loop.start("t1")
    await loop.feed_partial("tour un")
    await loop.join()
    assert state.latest() is not None

    # Arming a new turn clears the store (and resets seq → next snapshot seq=1).
    loop.start("t2")
    assert state.latest() is None


# --- defensive parse ---------------------------------------------------------


async def test_malformed_reply_drops_snapshot() -> None:
    client = _FakeThinkerClient(replies=["this is not json"])
    state = LiveTranscriptState()
    loop = _loop(client, state, _Clock())
    loop.start("t1")
    await loop.feed_partial("hello")
    await loop.join()
    assert state.latest() is None
    assert _snapshots() == []


def test_parse_snapshot_strips_code_fence() -> None:
    parsed = _parse_snapshot_json('```json\n{"corrected_text": "salut"}\n```')
    assert parsed is not None
    assert parsed.corrected_text == "salut"
    assert parsed.variables == {}


def test_parse_snapshot_coerces_wrong_types() -> None:
    parsed = _parse_snapshot_json(
        json.dumps(
            {
                "corrected_text": 123,  # wrong type → ""
                "variables": "nope",  # wrong type → {}
                "user_turn_complete": 1,  # int, not bool → False
                "backchannel": "  ",  # blank → None
            }
        )
    )
    assert parsed is not None
    assert parsed.corrected_text == ""
    assert parsed.variables == {}
    assert parsed.user_turn_complete is False
    assert parsed.backchannel is None


def test_parse_snapshot_non_object_returns_none() -> None:
    assert _parse_snapshot_json("[1, 2, 3]") is None
    assert _parse_snapshot_json("") is None


# --- lifecycle epoch (stale-pass guard) ---------------------------------------


async def test_stale_generation_pass_never_lands_snapshot() -> None:
    """A pass that outlives a hard_cancel + re-arm (same turn id) drops its result.

    ``hard_cancel`` never awaits the cancelled task, so a pass past its last
    await point can conclude AFTER a barge-in resume re-armed the SAME turn id
    (``turn_id`` does not discriminate there). The lifecycle epoch captured at
    launch must reject the stale landing.
    """

    state = LiveTranscriptState()
    client = _FakeThinkerClient()
    loop = _loop(client, state, _Clock())

    loop.start("t1")
    # Park a pass IN FLIGHT inside the model call — the dangerous window.
    client.gate = asyncio.Event()
    stale_pass = asyncio.create_task(loop._run_pass("salut bob", loop._generation))
    await client.started.wait()

    # Barge-in: zero-grace cancel (the task is never awaited) + resume re-arms
    # the SAME turn id.
    loop.hard_cancel()
    loop.start("t1")

    # The surviving pass resumes past its await and concludes against the
    # re-armed turn — the epoch guard must drop its snapshot.
    client.gate.set()
    await stale_pass
    assert state.latest() is None

    # A pass of the CURRENT epoch lands normally.
    client.gate = None
    await loop._run_pass("salut bob", loop._generation)
    snapshot = state.latest()
    assert snapshot is not None
    assert snapshot.corrected_text == "salut bob"


async def test_stale_generation_pass_never_reschedules_a_rerun() -> None:
    """A stale pass with a pending rerun must not relaunch into the new epoch."""

    state = LiveTranscriptState()
    client = _FakeThinkerClient()
    loop = _loop(client, state, _Clock())

    loop.start("t1")
    stale_generation = loop._generation
    loop._rerun = True
    loop._pending_text = "vieux partiel"
    loop.hard_cancel()
    loop.start("t1")
    loop._rerun = True
    loop._pending_text = "vieux partiel"

    await loop._maybe_rerun(stale_generation)
    assert loop.inflight is False
    assert client.calls == []
