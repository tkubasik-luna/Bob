"""Tracer-bullet tests for the reasoning stream channel (PRD 0011 / issue 0069).

Two surfaces:

1. :class:`bob.sub_agent.reasoning_stream.ReasoningStreamReader` — reasoning
   deltas exposed in order AND the final content separated; degraded-mode
   (no reasoning channel) signal exposed.
2. :class:`bob.sub_agent.runner.SubAgentRunner` NON-REGRESSION — with a noisy
   reasoning stream running in parallel, the ``SubAgentAction`` is parsed and
   validated ONLY from the final aggregated content; reasoning never
   contaminates parsing and a ``reasoning_delta`` WS event is emitted per delta,
   tagged by ``agent_ref``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from bob import ws_events
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.llm.types import LLMResponse, StreamChunk, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent import (
    SubAgentPolicy,
    SubAgentRunner,
    SubAgentToolDefinition,
    SubAgentToolHandlerOutcome,
    SubAgentToolRegistry,
)
from bob.sub_agent.reasoning_stream import (
    ReasoningStreamReader,
    ReasoningStreamReaderError,
)
from bob.task_store import TaskStore


async def _emit(chunks: list[StreamChunk]) -> AsyncIterator[StreamChunk]:
    for chunk in chunks:
        yield chunk


# ---------------------------------------------------------------------------
# ReasoningStreamReader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reader_separates_reasoning_and_content_in_order() -> None:
    """Reasoning deltas exposed in order; final content aggregated separately."""

    content_obj = json.dumps({"action": "done", "result_summary": "ok"})
    chunks = [
        StreamChunk(kind="reasoning", reasoning_delta="Let me "),
        StreamChunk(kind="reasoning", reasoning_delta="think... "),
        StreamChunk(kind="text", text_delta=content_obj[:10]),
        StreamChunk(kind="reasoning", reasoning_delta="done now"),
        StreamChunk(kind="text", text_delta=content_obj[10:]),
    ]
    reader = ReasoningStreamReader(_emit(chunks))

    seen: list[str] = []
    async for delta in reader.reasoning_deltas():
        seen.append(delta)

    assert seen == ["Let me ", "think... ", "done now"]
    assert reader.content == content_obj
    assert reader.degraded is False


@pytest.mark.asyncio
async def test_reader_degraded_when_no_reasoning_channel() -> None:
    """A stream with no reasoning chunk leaves ``degraded`` True (0070 hook)."""

    content_obj = json.dumps({"action": "progress", "thought": "x"})
    reader = ReasoningStreamReader(_emit([StreamChunk(kind="text", text_delta=content_obj)]))

    seen = [d async for d in reader.reasoning_deltas()]

    assert seen == []
    assert reader.content == content_obj
    assert reader.degraded is True


@pytest.mark.asyncio
async def test_reader_guards_against_reading_before_drain() -> None:
    """Reading ``content`` / ``degraded`` before draining raises (no partial parse)."""

    reader = ReasoningStreamReader(_emit([StreamChunk(kind="text", text_delta="x")]))
    with pytest.raises(ReasoningStreamReaderError):
        _ = reader.content
    with pytest.raises(ReasoningStreamReaderError):
        _ = reader.degraded


# ---------------------------------------------------------------------------
# SubAgentRunner — action-from-final-content NON-REGRESSION
# ---------------------------------------------------------------------------


class _StreamingScriptedClient(LLMClient):
    """Scripts ``stream_chat`` with interleaved reasoning + content chunks.

    Each entry of ``streams`` is replayed in FIFO order. ``stream_chat`` is the
    only method the runner calls; ``chat`` / ``complete`` raise so a regression
    that reverts the runner to the non-streaming path fails loudly here.
    """

    def __init__(self, *, streams: list[list[StreamChunk]], guided: bool = False) -> None:
        self._streams = list(streams)
        self._guided = guided
        self.stream_calls: list[dict[str, Any]] = []

    def supports_guided_json(self) -> bool:
        return self._guided

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        raise AssertionError("runner must use stream_chat, not chat")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls.append({"schema": schema, "session_id": session_id})
        if not self._streams:
            raise AssertionError("ran out of scripted streams")
        return _emit(self._streams.pop(0))


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _make_running_task(store: TaskStore, *, goal: str = "do the thing") -> str:
    task_id = store.create_task(title="t", goal=goal)
    store.update_state(task_id, "running")
    return task_id


def _content_chunks(payload: dict[str, Any]) -> list[StreamChunk]:
    """Split a JSON action payload into several ``text`` chunks (streamed shape)."""

    raw = json.dumps(payload)
    mid = len(raw) // 2
    return [
        StreamChunk(kind="text", text_delta=raw[:mid]),
        StreamChunk(kind="text", text_delta=raw[mid:]),
    ]


@pytest.mark.asyncio
async def test_action_parsed_only_from_content_despite_noisy_reasoning() -> None:
    """NON-REGRESSION: a noisy reasoning stream does not contaminate the action.

    The reasoning channel emits text that, if it leaked into the parse, is NOT a
    valid action envelope ("I should call done..."). The runner must reach a
    clean terminal ``done`` parsed solely from the aggregated content, and a
    ``reasoning_delta`` WS event must be emitted per reasoning delta, tagged by
    ``agent_ref`` = task_id.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    # A done action split across two text chunks, with noisy reasoning
    # interleaved between/around them.
    done_payload = {
        "action": "done",
        "result_summary": "the answer is 42",
        "status": "complete",
        "reason_code": "ok",
        "cost": {},
    }
    raw = json.dumps(done_payload)
    mid = len(raw) // 2
    stream = [
        StreamChunk(kind="reasoning", reasoning_delta="Hmm, I should "),
        StreamChunk(kind="text", text_delta=raw[:mid]),
        StreamChunk(kind="reasoning", reasoning_delta="emit a done action {fake json}"),
        StreamChunk(kind="text", text_delta=raw[mid:]),
        StreamChunk(kind="reasoning", reasoning_delta=" — yes."),
    ]
    client = _StreamingScriptedClient(streams=[stream])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=10_000),
    )

    emitted: list[dict[str, Any]] = []

    async def _capture(event: dict[str, Any]) -> None:
        emitted.append(event)

    ws_events.set_emitter(_capture)
    try:
        await runner.run(task_id)
    finally:
        ws_events.set_emitter(None)

    # Action parsed cleanly from the aggregated content → terminal done.
    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "the answer is 42"

    # Every reasoning chunk reaches the wire in order, tagged by agent_ref.
    # Issue 0123 coalesces the per-token frames into merged ``reasoning_delta``
    # events (same wire type, deltas concatenated) per batching window — the
    # full text and its order are the contract, not the frame cadence.
    reasoning_events = [e for e in emitted if e.get("type") == "reasoning_delta"]
    assert (
        "".join(e["delta"] for e in reasoning_events)
        == "Hmm, I should emit a done action {fake json} — yes."
    )
    assert all(e["agent_ref"] == task_id for e in reasoning_events)


@pytest.mark.asyncio
async def test_validation_retry_unchanged_with_streaming() -> None:
    """NON-REGRESSION: an invalid first content envelope still triggers the
    validator retry from the AGGREGATED content, then converges to done.

    Reasoning on the first (invalid) iteration must not rescue the bad content
    nor short-circuit the retry — the loop behaves exactly as the non-streaming
    ``chat`` path did before streaming.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    invalid = [
        StreamChunk(kind="reasoning", reasoning_delta="thinking hard"),
        StreamChunk(kind="text", text_delta="this is not json at all"),
    ]
    valid = _content_chunks(
        {
            "action": "done",
            "result_summary": "recovered",
            "status": "complete",
            "reason_code": "ok",
            "cost": {},
        }
    )
    client = _StreamingScriptedClient(streams=[invalid, valid])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=10_000),
    )

    ws_events.set_emitter(None)
    await runner.run(task_id)

    # Two stream calls: the invalid one + the retry that recovered.
    assert len(client.stream_calls) == 2
    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "recovered"


# ---------------------------------------------------------------------------
# SubAgentRunner — agent_activity chip EMISSION (PRD 0011 / issue 0071)
# ---------------------------------------------------------------------------


class _EchoArgs(BaseModel):
    value: str


def _registry_with_echo() -> SubAgentToolRegistry:
    async def _handler(_ctx: Any, args: BaseModel) -> SubAgentToolHandlerOutcome:
        assert isinstance(args, _EchoArgs)
        return SubAgentToolHandlerOutcome(status="ok", result={"echo": args.value})

    return SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="echo",
                version="v1",
                description="echoes value",
                args_model=_EchoArgs,
                handler=_handler,
            )
        ]
    )


async def _run_capturing(runner: SubAgentRunner, task_id: str) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []

    async def _capture(event: dict[str, Any]) -> None:
        emitted.append(event)

    ws_events.set_emitter(_capture)
    try:
        await runner.run(task_id)
    finally:
        ws_events.set_emitter(None)
    return emitted


@pytest.mark.asyncio
async def test_runner_emits_started_toolcall_finished_chips() -> None:
    """A tool-call run surfaces started → tool_call(running, ok) → finished chips."""

    store = _make_store()
    task_id = _make_running_task(store)

    tool_call = _content_chunks({"action": "tool_call", "name": "echo", "args": {"value": "hi"}})
    done = _content_chunks(
        {
            "action": "done",
            "result_summary": "ok",
            "status": "complete",
            "reason_code": "ok",
            "cost": {},
        }
    )
    client = _StreamingScriptedClient(streams=[tool_call, done])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        tool_registry=_registry_with_echo(),
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=10_000),
    )

    emitted = await _run_capturing(runner, task_id)
    chips = [e for e in emitted if e.get("type") == "agent_activity"]
    # Every chip is tagged by the producing agent.
    assert all(c["agent_ref"] == task_id for c in chips)
    kinds = [(c["kind"], c["status"]) for c in chips]
    # In chronological order: started, the goal-driven tool-retrieval marker
    # (PRD 0015 / issue 0092, emitted once before the first turn), tool_call
    # running, tool_call ok, finished.
    assert kinds == [
        ("started", "info"),
        ("tool_retrieval", "info"),
        ("tool_call", "running"),
        ("tool_call", "ok"),
        ("finished", "ok"),
    ]
    # The tool-call chip labels carry the tool NAME, never any result body.
    tool_labels = [c["label"] for c in chips if c["kind"] == "tool_call"]
    assert all("echo" in label for label in tool_labels)
    assert all("hi" not in label for label in tool_labels)


@pytest.mark.asyncio
async def test_runner_emits_validation_failed_and_retry_chips_on_bad_output() -> None:
    """An invalid envelope surfaces a validation_failed chip + a retry chip.

    A PASSING parse on the recovery iteration must NOT add its own validation
    chip (aggregation / suppression), so exactly ONE validation_failed chip
    appears across the run.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    invalid = [StreamChunk(kind="text", text_delta="not json")]
    valid = _content_chunks(
        {
            "action": "done",
            "result_summary": "recovered",
            "status": "complete",
            "reason_code": "ok",
            "cost": {},
        }
    )
    client = _StreamingScriptedClient(streams=[invalid, valid])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=10_000),
    )

    emitted = await _run_capturing(runner, task_id)
    chips = [e for e in emitted if e.get("type") == "agent_activity"]
    kinds = [c["kind"] for c in chips]

    assert kinds.count("validation_failed") == 1
    assert kinds.count("retry") == 1
    # The salient incident precedes the retry it triggered.
    assert kinds.index("validation_failed") < kinds.index("retry")
    # No passing-validation chip leaked in (there is no "validation_passed" kind;
    # a PASS is suppressed at the projector).
    assert "validation_passed" not in kinds
    task = store.get_task(task_id)
    assert task.state == "done"


# ---------------------------------------------------------------------------
# SubAgentRunner — narrated-steps fallback in degraded mode (PRD 0011 / issue 0070)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_progress_thought_narrated_as_reasoning_delta() -> None:
    """No reasoning channel → the progress thought is emitted as narrated feed text.

    Issue 0070. A model/endpoint that streams NO ``reasoning_content`` leaves the
    reader ``degraded`` (no raw reasoning deltas). The feed must NOT be empty: the
    sub-agent's own ``progress`` thought is surfaced on the SAME
    ``reasoning_delta`` channel (tagged with the agent_ref) so the AgentBlock has
    readable text. The streamed-vs-narrated switch is per-iteration.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    # Iteration 1: a progress action with a thought, NO reasoning chunks (degraded).
    progress = _content_chunks({"action": "progress", "thought": "Je lis le mail."})
    done = _content_chunks(
        {
            "action": "done",
            "result_summary": "fini",
            "status": "complete",
            "reason_code": "ok",
            "cost": {},
        }
    )
    client = _StreamingScriptedClient(streams=[progress, done])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=10_000),
    )

    emitted = await _run_capturing(runner, task_id)

    # The raw channel produced NO reasoning deltas (no reasoning chunk in stream),
    # but the narrated progress thought reached the feed as a reasoning_delta.
    reasoning_events = [e for e in emitted if e.get("type") == "reasoning_delta"]
    assert [e["delta"] for e in reasoning_events] == ["Je lis le mail."]
    assert all(e["agent_ref"] == task_id for e in reasoning_events)


@pytest.mark.asyncio
async def test_non_degraded_progress_thought_not_duplicated_as_narration() -> None:
    """Reasoning IS streamed → the progress thought is NOT also narrated (no dup).

    Issue 0070 acceptance: when the iteration carried a live reasoning channel,
    only the streamed reasoning deltas carry text. The progress thought must NOT
    be re-emitted as a narrated ``reasoning_delta`` — that would double the text.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    # Iteration 1: progress WITH a real reasoning channel (not degraded).
    raw = json.dumps({"action": "progress", "thought": "Je lis le mail."})
    mid = len(raw) // 2
    progress = [
        StreamChunk(kind="reasoning", reasoning_delta="reflexion live"),
        StreamChunk(kind="text", text_delta=raw[:mid]),
        StreamChunk(kind="text", text_delta=raw[mid:]),
    ]
    done = _content_chunks(
        {
            "action": "done",
            "result_summary": "fini",
            "status": "complete",
            "reason_code": "ok",
            "cost": {},
        }
    )
    client = _StreamingScriptedClient(streams=[progress, done])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        policy=SubAgentPolicy(max_iterations=5, wall_clock_seconds=999.0, token_cap=10_000),
    )

    emitted = await _run_capturing(runner, task_id)

    reasoning_events = [e for e in emitted if e.get("type") == "reasoning_delta"]
    # ONLY the live reasoning delta carries text — the thought is not duplicated.
    assert [e["delta"] for e in reasoning_events] == ["reflexion live"]
    assert "Je lis le mail." not in [e["delta"] for e in reasoning_events]
