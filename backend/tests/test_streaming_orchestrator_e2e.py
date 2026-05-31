"""End-to-end streaming test (PRD 0006 / issue 0049).

Drives the orchestrator through a scripted streaming-fake-LLM and
asserts the WS frame sequence reaches a recording emitter in the right
order:

- one or more ``speech_delta`` frames as ``say.speech`` accumulates,
- exactly one ``ui_payload`` frame on argument-object close (when ``ui``
  is non-null),
- a final ``assistant_msg`` carrying the same ``msg_id`` as the streamed
  deltas.

The streaming-fake-LLM uses the existing :class:`FakeLLMClient`
harness with the ``stream_responses`` list pre-loaded — no parallel
client class.

We also assert the "first speech_delta arrives before the final
``assistant_msg``" timing relationship (a relative budget — see PRD
'overkill robust' notes on hardware variance). The wall-clock
``< 500 ms`` smoke target from the issue acceptance criteria is checked
on the dev box with a generous floor in the actual test (5 s ceiling)
because CI containers run colder; the real value of the assertion is
the ordering, not the wall time.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from bob import event_bus_v2
from bob.context.policy import legacy_full_history_policy
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse, StreamChunk, ToolCall, ToolDefinition
from bob.orchestrator import Orchestrator
from bob.task_store import TaskStore

from ._harness.fake_llm import FakeLLMClient

_TEST_JARVIS_PROMPT = "Tu es Jarvis-de-test, ton calme et concis."


class _RecordingScheduler:
    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store

    async def enqueue(self, task_id: str) -> None: ...
    async def resume(self, task_id: str) -> None: ...
    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None: ...


def _build_orchestrator(client: FakeLLMClient) -> tuple[Orchestrator, JarvisStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler(task_store)
    orchestrator = Orchestrator(
        jarvis_client=client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        task_scheduler=scheduler,
        jarvis_prompt=_TEST_JARVIS_PROMPT,
        context_policy=legacy_full_history_policy(),
    )
    return orchestrator, jarvis_store


@pytest.fixture
def recorder() -> Iterator[list[dict[str, Any]]]:
    """Install a recording WS emitter on :mod:`bob.event_bus_v2`.

    Returns the list of captured payloads. The fixture restores the
    previous emitter (``None`` in test mode) at teardown so subsequent
    tests don't leak the recorder.
    """

    captured: list[dict[str, Any]] = []

    async def _record(payload: dict[str, Any]) -> None:
        captured.append(payload)

    event_bus_v2.set_ws_emitter(_record)
    yield captured
    event_bus_v2.set_ws_emitter(None)


def _scripted_say_stream(
    call_id: str = "call_say",
    speech_chunks: list[str] | None = None,
    ui: dict[str, Any] | None = None,
) -> list[StreamChunk]:
    """Build the chunk sequence a streaming LM Studio would emit for ``say``.

    The chunks together form the JSON literal:
        ``{"speech":"<concat speech_chunks>","ui":<ui-or-null>}``

    ``ui`` is included only when non-None; the LLM-streamed JSON omits
    the key entirely in the "no overlay" case (matches the unified
    ``say`` tool's optional ``ui`` argument).
    """

    if speech_chunks is None:
        speech_chunks = ["Bonjour ", "Tom, ", "comment ", "ça ", "va ?"]

    parts: list[str] = ['{"speech":"']
    for piece in speech_chunks:
        parts.append(piece)
    parts.append('"')
    if ui is not None:
        import json

        parts.append(",")
        parts.append('"ui":')
        parts.append(json.dumps(ui, ensure_ascii=False))
    parts.append("}")

    # Stream the JSON in micro-chunks roughly matching what an LM
    # Studio streaming response looks like.
    chunks: list[StreamChunk] = [
        StreamChunk(kind="tool_call_start", tool_call_id=call_id, name="say"),
    ]
    for part in parts:
        chunks.append(
            StreamChunk(kind="tool_call_args_delta", tool_call_id=call_id, args_delta=part)
        )
    import json

    # ``final_arguments`` mirrors what the LLM client would parse on close.
    full_text = "".join(parts)
    chunks.append(
        StreamChunk(
            kind="tool_call_end",
            tool_call_id=call_id,
            final_arguments=json.loads(full_text),
        )
    )
    return chunks


@pytest.mark.asyncio
async def test_e2e_streaming_say_emits_deltas_then_ui_payload(
    recorder: list[dict[str, Any]],
) -> None:
    """Full pipeline: scripted stream → deltas + ui_payload + assistant_msg."""

    client = FakeLLMClient(
        stream_responses=[
            _scripted_say_stream(
                speech_chunks=["Bonjour ", "Tom"],
                ui={"component": "Markdown", "props": {"content": "# Salut"}},
            )
        ]
    )
    orchestrator, _js = _build_orchestrator(client)
    response = await orchestrator.process_user_message("s1", "hi")

    # The orchestrator returned the full speech + ui + a msg_id.
    assert response.speech == "Bonjour Tom"
    assert len(response.ui) == 1
    assert response.ui[0].component == "Markdown"
    assert response.msg_id  # non-empty

    speech_deltas = [e for e in recorder if e["type"] == "speech_delta"]
    ui_payloads = [e for e in recorder if e["type"] == "ui_payload"]

    # At least one speech_delta + exactly one ui_payload.
    assert len(speech_deltas) >= 1
    assert len(ui_payloads) == 1

    # Concatenated deltas reconstruct the spoken text.
    assert "".join(d["delta"] for d in speech_deltas) == "Bonjour Tom"

    # ui_payload carries the right shape + msg_id.
    assert ui_payloads[0]["ui"] == {
        "component": "Markdown",
        "props": {"content": "# Salut"},
    }
    assert ui_payloads[0]["msg_id"] == response.msg_id
    for delta_frame in speech_deltas:
        assert delta_frame["msg_id"] == response.msg_id

    # Frame sequence: every speech_delta strictly precedes the
    # ui_payload (frame ordering preserves the LLM-stream insertion
    # order at the bus).
    speech_idxs = [i for i, e in enumerate(recorder) if e["type"] == "speech_delta"]
    ui_idx = next(i for i, e in enumerate(recorder) if e["type"] == "ui_payload")
    assert all(idx < ui_idx for idx in speech_idxs)


@pytest.mark.asyncio
async def test_e2e_streaming_say_without_ui_emits_no_payload(
    recorder: list[dict[str, Any]],
) -> None:
    """A ``say`` call without a ``ui`` payload emits zero overlay frames."""

    client = FakeLLMClient(stream_responses=[_scripted_say_stream(speech_chunks=["Hi"], ui=None)])
    orchestrator, _js = _build_orchestrator(client)
    await orchestrator.process_user_message("s1", "salut")

    ui_payloads = [e for e in recorder if e["type"] == "ui_payload"]
    assert ui_payloads == []
    # Speech still streams.
    speech_deltas = [e for e in recorder if e["type"] == "speech_delta"]
    assert len(speech_deltas) >= 1


@pytest.mark.asyncio
async def test_e2e_streaming_first_delta_lands_before_stream_completes(
    recorder: list[dict[str, Any]],
) -> None:
    """Relative timing budget: a speech_delta lands BEFORE the final return.

    The PRD acceptance criterion is "< 500 ms from user submit to first
    speech_delta on the dev box". On a CI container we can't pin a wall
    clock that tightly without flake; we assert the *ordering* invariant
    instead: at least one speech_delta is buffered on the recorder
    while the orchestrator is still processing. We do that by injecting
    a tiny ``asyncio.sleep`` between the ``tool_call_args_delta`` and the
    ``tool_call_end`` so the streamed deltas land first.
    """

    seen_speech_at: list[float] = []

    async def _stamp_recorder(payload: dict[str, Any]) -> None:
        if payload.get("type") == "speech_delta":
            seen_speech_at.append(time.perf_counter())
            recorder.append(payload)

    event_bus_v2.set_ws_emitter(_stamp_recorder)
    try:
        # Wrap the streamed chunks with a tiny delay between args_delta
        # and tool_call_end so the relative timing assertion is
        # observable from outside.
        async def _delayed_stream() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(kind="tool_call_start", tool_call_id="c", name="say")
            yield StreamChunk(
                kind="tool_call_args_delta",
                tool_call_id="c",
                args_delta='{"speech":"Hi"}',
            )
            # Yield control + measurable wall-time delay so the
            # recorder definitely sees the speech_delta before
            # tool_call_end is observed.
            await asyncio.sleep(0.05)
            yield StreamChunk(
                kind="tool_call_end",
                tool_call_id="c",
                final_arguments={"speech": "Hi"},
            )

        class _DelayedClient(FakeLLMClient):
            async def stream_complete(
                self,
                messages: list[dict[str, Any]],
                tools: list[ToolDefinition] | None = None,
                session_id: str | None = None,
            ) -> AsyncIterator[StreamChunk]:
                self.stream_calls.append(
                    {"messages": messages, "tools": tools, "session_id": session_id}
                )
                return _delayed_stream()

        client = _DelayedClient()
        orchestrator, _js = _build_orchestrator(client)
        t_start = time.perf_counter()
        await orchestrator.process_user_message("s1", "hi")
        t_end = time.perf_counter()

        assert seen_speech_at, "No speech_delta was recorded"
        first_delta_ms = (seen_speech_at[0] - t_start) * 1000.0
        total_ms = (t_end - t_start) * 1000.0
        # First delta lands before the orchestrator returns — the
        # whole point of streaming.
        assert seen_speech_at[0] < t_end
        # Sanity ceiling — if this trips, something is wrong with the
        # streaming machinery (CI floor: 5 s).
        assert first_delta_ms < 5_000.0
        assert total_ms < 10_000.0
    finally:
        event_bus_v2.set_ws_emitter(None)


@pytest.mark.asyncio
async def test_e2e_streaming_msg_id_consistent_with_assistant_msg(
    recorder: list[dict[str, Any]],
) -> None:
    """The ``msg_id`` field is constant across all streamed frames + response."""

    client = FakeLLMClient(
        stream_responses=[
            _scripted_say_stream(
                speech_chunks=["A", "B"],
                ui={"component": "Markdown", "props": {"content": "x"}},
            )
        ]
    )
    orchestrator, _js = _build_orchestrator(client)
    response = await orchestrator.process_user_message("s1", "hi")
    streamed_ids = {e["msg_id"] for e in recorder if e["type"] in ("speech_delta", "ui_payload")}
    assert streamed_ids == {response.msg_id}


@pytest.mark.asyncio
async def test_e2e_jarvis_lane_reasoning_chip_and_final_answer(
    recorder: list[dict[str, Any]],
) -> None:
    """PRD 0011 / issue 0072 — Jarvis gets its own feed lane (``agent_ref="jarvis"``).

    A streamed Jarvis ``say`` turn that ALSO carries native ``reasoning_content``
    chunks must surface, on the SAME user-facing channels the sub-agents use:

    1. ``reasoning_delta`` frames tagged ``agent_ref="jarvis"`` (the live
       chain-of-thought), AND
    2. the final spoken answer duplicated as TEXT into the lane (also a
       ``reasoning_delta`` with ``agent_ref="jarvis"``, emitted once at the end),

    WITHOUT breaking the existing ``speech_delta`` → sphere/TTS path.
    """

    call_id = "call_say"
    chunks: list[StreamChunk] = [
        StreamChunk(kind="tool_call_start", tool_call_id=call_id, name="say"),
        # Native reasoning interleaved with the argument stream (cosmetic).
        StreamChunk(kind="reasoning", reasoning_delta="L'utilisateur "),
        StreamChunk(kind="reasoning", reasoning_delta="dit bonjour."),
        StreamChunk(kind="tool_call_args_delta", tool_call_id=call_id, args_delta='{"speech":"'),
        StreamChunk(kind="tool_call_args_delta", tool_call_id=call_id, args_delta="Salut "),
        StreamChunk(kind="tool_call_args_delta", tool_call_id=call_id, args_delta="Tom"),
        StreamChunk(kind="tool_call_args_delta", tool_call_id=call_id, args_delta='"}'),
        StreamChunk(
            kind="tool_call_end",
            tool_call_id=call_id,
            final_arguments={"speech": "Salut Tom"},
        ),
    ]
    client = FakeLLMClient(stream_responses=[chunks])
    orchestrator, _js = _build_orchestrator(client)
    response = await orchestrator.process_user_message("s1", "bonjour")

    assert response.speech == "Salut Tom"

    # Speech still streams to the sphere/TTS path, unchanged.
    speech_deltas = [e for e in recorder if e["type"] == "speech_delta"]
    assert "".join(d["delta"] for d in speech_deltas) == "Salut Tom"

    # Jarvis-lane reasoning + final answer ride ``reasoning_delta`` with the
    # fixed ``agent_ref="jarvis"`` (distinct from any sub-task lane).
    jarvis_reasoning = [
        e
        for e in recorder
        if e["type"] == "reasoning_delta" and e.get("agent_ref") == "jarvis"
    ]
    joined = "".join(d["delta"] for d in jarvis_reasoning)
    # Live chain-of-thought present...
    assert "L'utilisateur dit bonjour." in joined
    # ...and the final answer duplicated into the lane, AFTER the reasoning.
    assert joined.endswith("Salut Tom")


@pytest.mark.asyncio
async def test_e2e_jarvis_lane_degraded_no_reasoning_still_has_answer(
    recorder: list[dict[str, Any]],
) -> None:
    """A non-reasoning Jarvis stream still lands the final answer in the lane.

    Mirrors the sub-agent degraded fallback (issue 0070): no ``reasoning``
    chunk means no live chain-of-thought, but the lane is never empty — the
    settled answer is still duplicated in as ``reasoning_delta`` text, and the
    ``speech_delta`` → TTS path is unaffected.
    """

    client = FakeLLMClient(stream_responses=[_scripted_say_stream(speech_chunks=["Coucou"])])
    orchestrator, _js = _build_orchestrator(client)
    await orchestrator.process_user_message("s1", "hi")

    jarvis_reasoning = [
        e
        for e in recorder
        if e["type"] == "reasoning_delta" and e.get("agent_ref") == "jarvis"
    ]
    assert "".join(d["delta"] for d in jarvis_reasoning) == "Coucou"
    speech_deltas = [e for e in recorder if e["type"] == "speech_delta"]
    assert "".join(d["delta"] for d in speech_deltas) == "Coucou"


@pytest.mark.asyncio
async def test_e2e_jarvis_lane_delegate_emits_orchestration_chip(
    recorder: list[dict[str, Any]],
) -> None:
    """A ``spawn_task`` turn surfaces a Jarvis orchestration chip in the lane.

    PRD 0011 / issue 0072 — when Jarvis delegates, an ``agent_activity`` chip
    (``agent_ref="jarvis"``, ``kind="tool_call"``) naming the delegated task
    appears in the Jarvis lane.
    """

    spawn_call = ToolCall(
        id="call_spawn",
        name="spawn_task",
        arguments={"title": "Exposé", "goal": "Rédige un long exposé"},
    )
    client = FakeLLMClient(
        complete_responses=[LLMResponse(text=None, tool_calls=[spawn_call])]
    )
    orchestrator, _js = _build_orchestrator(client)
    response = await orchestrator.process_user_message("s1", "fais un exposé")
    assert len(response.spawned_task_ids) == 1

    jarvis_chips = [
        e
        for e in recorder
        if e["type"] == "agent_activity" and e.get("agent_ref") == "jarvis"
    ]
    assert len(jarvis_chips) == 1
    chip = jarvis_chips[0]
    assert chip["kind"] == "tool_call"
    assert chip["status"] == "ok"
    assert chip["label"].startswith("délègue")
    assert "Exposé" in chip["label"]


@pytest.mark.asyncio
async def test_e2e_streaming_falls_back_to_complete_when_stream_unscripted(
    recorder: list[dict[str, Any]],
) -> None:
    """Tests that only scripted ``complete_responses`` still work end-to-end.

    Confirms the harness's fallback path: when no scripted stream is
    queued, the FakeLLMClient replays the next ``complete()`` response
    as a synthetic chunk trio. This keeps every pre-0049 orchestrator
    test green without rewriting them.
    """

    client = FakeLLMClient(
        complete_responses=[
            LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call_say",
                        name="say",
                        arguments={"speech": "Fallback speech"},
                    )
                ],
            )
        ]
    )
    orchestrator, _js = _build_orchestrator(client)
    response = await orchestrator.process_user_message("s1", "hi")
    assert response.speech == "Fallback speech"
    # Even via the fallback we still emit a speech_delta (the chunk
    # trio re-injects the full argument string as one args_delta).
    speech_deltas = [e for e in recorder if e["type"] == "speech_delta"]
    assert len(speech_deltas) >= 1
    assert "".join(d["delta"] for d in speech_deltas) == "Fallback speech"
