"""Tests for :mod:`bob.orchestrator`."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from bob import ws_events
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import LLMClient
from bob.orchestrator import (
    _SPAWN_CONFIRMATION,
    Orchestrator,
)
from bob.sub_agent_runner import SubAgentRunner
from bob.task_scheduler import TaskScheduler
from bob.task_store import TaskStore


class FakeLLMClient(LLMClient):
    """Scriptable LLMClient: returns canned ``complete()`` + ``chat()`` outputs."""

    def __init__(
        self,
        *,
        complete_responses: list[LLMResponse] | None = None,
        chat_responses: list[str] | None = None,
    ) -> None:
        self._complete_responses = list(complete_responses or [])
        self._chat_responses = list(chat_responses or [])
        self.chat_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.chat_calls.append({"messages": messages, "schema": schema, "session_id": session_id})
        if not self._chat_responses:
            raise AssertionError("FakeLLMClient ran out of canned chat() responses")
        return self._chat_responses.pop(0)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        self.complete_calls.append({"messages": messages, "tools": tools, "session_id": session_id})
        if not self._complete_responses:
            raise AssertionError("FakeLLMClient ran out of canned complete() responses")
        return self._complete_responses.pop(0)


_TEST_JARVIS_PROMPT = "Tu es Jarvis-de-test, ton calme et concis."


class _RecordingScheduler:
    """Stand-in for :class:`TaskScheduler` in orchestrator tests.

    Records each ``enqueue`` / ``resume`` / ``cancel`` call so tests can
    assert dispatch without spinning up real runner tasks. The fake
    transitions the task to ``running`` (on enqueue / resume) so existing
    assertions (``list_tasks(state="running")``) keep their meaning — that
    is the contract the real scheduler honours when a slot is free. The
    ``cancel`` fake is non-transitioning: tests asserting the dispatch only
    care that the call landed with the right args.
    """

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store
        self.enqueued: list[str] = []
        self.resumed: list[str] = []
        self.cancelled: list[tuple[str, str]] = []

    async def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)
        self._task_store.update_state(task_id, "running")

    async def resume(self, task_id: str) -> None:
        self.resumed.append(task_id)
        self._task_store.update_state(task_id, "running")

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        self.cancelled.append((task_id, reason))


def _make_orchestrator(
    *,
    complete_responses: list[LLMResponse] | None = None,
    chat_responses: list[str] | None = None,
    scheduler: Any = None,
) -> tuple[Orchestrator, FakeLLMClient, FakeLLMClient, JarvisStore, TaskStore, Any]:
    jarvis_client = FakeLLMClient(
        complete_responses=complete_responses,
        chat_responses=chat_responses,
    )
    subagent_client = FakeLLMClient()
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    if scheduler is None:
        scheduler = _RecordingScheduler(task_store)
    orchestrator = Orchestrator(
        jarvis_client=jarvis_client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        task_scheduler=scheduler,
        jarvis_prompt=_TEST_JARVIS_PROMPT,
    )
    return orchestrator, jarvis_client, subagent_client, jarvis_store, task_store, scheduler


def _spawn_tool_call(*, title: str = "Buy milk", goal: str = "Acheter du lait") -> ToolCall:
    return ToolCall(
        id=f"call_{uuid4().hex[:6]}",
        name="spawn_task",
        arguments={"title": title, "goal": goal},
    )


def _say_tool_call(
    *,
    speech: str = "Bonjour Tom",
    ui: dict[str, Any] | None = None,
) -> ToolCall:
    """Build a ``say(speech, ui)`` :class:`ToolCall` (issue 0047)."""

    args: dict[str, Any] = {"speech": speech}
    if ui is not None:
        args["ui"] = ui
    return ToolCall(
        id=f"call_{uuid4().hex[:6]}",
        name="say",
        arguments=args,
    )


def _say_response(speech: str = "Bonjour Tom", ui: dict[str, Any] | None = None) -> LLMResponse:
    """Build a Jarvis :class:`LLMResponse` wrapping a single ``say`` call."""

    return LLMResponse(text=None, tool_calls=[_say_tool_call(speech=speech, ui=ui)])


def _valid_payload(speech: str = "Bonjour Tom") -> str:
    return json.dumps({"speech": speech, "ui": []})


# ---------------------------------------------------------------------------
# Spawn path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_user_message_spawns_subtask_when_tool_call() -> None:
    orchestrator, jarvis_client, _sub_client, jarvis_store, task_store, scheduler = (
        _make_orchestrator(
            complete_responses=[
                LLMResponse(
                    text=None,
                    tool_calls=[_spawn_tool_call(title="Drafts", goal="Draft 3 thanks emails")],
                )
            ],
        )
    )

    response = await orchestrator.process_user_message("s1", "Draft 3 thanks emails")

    assert response.speech == _SPAWN_CONFIRMATION
    assert response.ui == []
    assert len(response.spawned_task_ids) == 1
    assert response.spawned_task_ids == scheduler.enqueued

    running = task_store.list_tasks(state="running")
    assert len(running) == 1
    assert running[0].title == "Drafts"
    assert running[0].goal == "Draft 3 thanks emails"

    history = jarvis_store.history()
    assert history == [
        {"role": "user", "content": "Draft 3 thanks emails"},
        {"role": "assistant", "content": _SPAWN_CONFIRMATION},
    ]

    # Spawn path must not fall through to the chat()-based structured path.
    assert jarvis_client.chat_calls == []
    assert len(jarvis_client.complete_calls) == 1


@pytest.mark.asyncio
async def test_process_user_message_spawns_v2_task_when_tool_call() -> None:
    """Regression: a v2 ``spawn_task`` call is confirmed, not silently crashed.

    Pre-fix ``_collect_dispatch_result`` only bucketed the v1 ``*_subtask``
    names, so a v2 ``spawn_task`` (the canonical entry point the prompt now
    advertises) left ``spawned`` empty and tripped ``assert say_speech is not
    None``. The AssertionError bubbled up, the spawn was never announced, and
    the crash was invisible (swallowed by the ws_router catch-all + the
    structlog bridge bypass).
    """

    spawn_task_call = ToolCall(
        id="call_v2",
        name="spawn_task",
        arguments={"title": "Exposé", "goal": "Rédige un long exposé"},
    )
    orchestrator, _jarvis_client, _sub_client, jarvis_store, _task_store, scheduler = (
        _make_orchestrator(
            complete_responses=[LLMResponse(text=None, tool_calls=[spawn_task_call])],
        )
    )

    response = await orchestrator.process_user_message("s1", "Fais un long exposé")

    assert response.speech == _SPAWN_CONFIRMATION
    assert len(response.spawned_task_ids) == 1
    assert response.spawned_task_ids == scheduler.enqueued
    assert jarvis_store.history()[-1] == {
        "role": "assistant",
        "content": _SPAWN_CONFIRMATION,
    }


@pytest.mark.asyncio
async def test_spawn_with_invalid_args_then_invalid_again_degrades() -> None:
    """Issue 0048: every dispatch errored across the retry budget → degrade speech.

    Pre-0048 (the issue 0047 contract) every-dispatch-errored raised
    :class:`OrchestratorContractError`. Issue 0048 wraps the dispatcher
    in a per-tool retry budget: one retry is allowed; if both attempts
    error the orchestrator emits the hardcoded degrade speech through
    the live :class:`SayTool` via the dispatcher (so the JarvisStore
    persistence + the ``jarvis.route`` event still fire).
    """

    bad_call = ToolCall(
        id="call_bad",
        name="spawn_task",
        arguments={"title": "no goal here"},
    )
    orchestrator, jarvis_client, _sub_client, jarvis_store, task_store, scheduler = (
        _make_orchestrator(
            complete_responses=[
                LLMResponse(text=None, tool_calls=[bad_call]),
                LLMResponse(text=None, tool_calls=[bad_call]),
            ],
        )
    )

    response = await orchestrator.process_user_message("s1", "anything")

    assert response.speech == "Désolé, peux-tu reformuler ?"
    assert task_store.list_tasks() == []
    assert scheduler.enqueued == []
    # Two complete() calls: initial + retry. The validator never asks
    # for a 3rd round; the degrade speech is dispatched in-process.
    assert len(jarvis_client.complete_calls) == 2
    # Issue 0047: the free-form ``chat()`` reply path was removed.
    assert jarvis_client.chat_calls == []
    # The hardcoded speech was dispatched through the SayTool → it
    # landed in the Jarvis store.
    history = jarvis_store.history()
    assert any(row["content"] == "Désolé, peux-tu reformuler ?" for row in history)


# ---------------------------------------------------------------------------
# Unified ``say`` tool — direct-reply path (issue 0047)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_say_tool_call_produces_direct_reply() -> None:
    """A ``say`` tool call surfaces as the assistant turn (no ``chat()`` round-trip)."""

    orchestrator, jarvis_client, _sub, jarvis_store, task_store, _scheduler = _make_orchestrator(
        complete_responses=[
            _say_response(
                speech="Salut Tom",
                ui={"component": "Markdown", "props": {"content": "**hi**"}},
            )
        ],
    )

    response = await orchestrator.process_user_message("s1", "Coucou")

    assert response.speech == "Salut Tom"
    assert response.spawned_task_ids == []
    assert len(response.ui) == 1
    assert response.ui[0].component == "Markdown"
    assert response.ui[0].props == {"content": "**hi**"}

    assert task_store.list_tasks() == []
    assert jarvis_store.history() == [
        {"role": "user", "content": "Coucou"},
        {"role": "assistant", "content": "Salut Tom"},
    ]
    # Issue 0047: every turn dispatches through ``complete()``. The
    # structured ``chat()`` reply path is gone.
    assert jarvis_client.chat_calls == []
    assert len(jarvis_client.complete_calls) == 1


@pytest.mark.asyncio
async def test_say_with_null_ui_omits_components() -> None:
    """``say(speech, ui=null)`` produces an empty ``OrchestratorResponse.ui``."""

    orchestrator, _jc, _sub, jarvis_store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[_say_response(speech="ok", ui=None)],
    )

    response = await orchestrator.process_user_message("s1", "ping")

    assert response.speech == "ok"
    assert response.ui == []
    assert jarvis_store.history()[-1] == {"role": "assistant", "content": "ok"}


@pytest.mark.asyncio
async def test_two_no_tool_call_attempts_degrade_to_hardcoded_say() -> None:
    """Issue 0048: contract violation across the retry budget → degrade speech.

    Pre-0048 a free-form text reply raised
    :class:`OrchestratorContractError` after a single attempt. Issue
    0048 wraps the LLM call in a per-tool retry budget: one retry is
    allowed; if both attempts still return free-form text the
    orchestrator emits the hardcoded ``Désolé, peux-tu reformuler ?``
    via the live :class:`SayTool` through the dispatcher.
    """

    orchestrator, jarvis_client, _sub, jarvis_store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[
            LLMResponse(text="first chatting", tool_calls=[]),
            LLMResponse(text="second chatting", tool_calls=[]),
        ],
    )

    response = await orchestrator.process_user_message("s1", "Coucou")

    assert response.speech == "Désolé, peux-tu reformuler ?"
    # User turn + degrade reply landed in the Jarvis store; nothing else.
    history = jarvis_store.history()
    assert history[0] == {"role": "user", "content": "Coucou"}
    assert history[-1]["content"] == "Désolé, peux-tu reformuler ?"
    # Two complete() round-trips: initial + 1 retry.
    assert len(jarvis_client.complete_calls) == 2
    # The structured ``chat()`` fallback path no longer fires.
    assert jarvis_client.chat_calls == []


@pytest.mark.asyncio
async def test_say_tool_call_uses_jarvis_prompt_in_system_message() -> None:
    """The single ``complete()`` call carries the personality + tools addendum."""

    orchestrator, jarvis_client, _sub, _store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[_say_response(speech="ok")],
    )

    await orchestrator.process_user_message("s1", "hi")

    complete_system = jarvis_client.complete_calls[0]["messages"][0]
    assert complete_system["role"] == "system"
    assert _TEST_JARVIS_PROMPT in complete_system["content"]
    # PRD 0006 / issue 0050: v2 task surface advertised by the addendum.
    assert "spawn_task" in complete_system["content"]
    # Issue 0047 (v2) closes the tool-call instruction loop.
    assert "say" in complete_system["content"]
    # No ``chat()`` round-trip.
    assert jarvis_client.chat_calls == []


# ---------------------------------------------------------------------------
# Tool registry surface delivered to ``complete()`` (issue 0044 + 0047)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_call_sends_unified_tool_surface() -> None:
    """The v2 task surface plus ``say`` + ``show_task_result`` are advertised.

    The v1 ``*_subtask`` aliases (issue 0044) have been removed — every
    call site uses the v2 names and the prompt no longer mentions them.
    """

    orchestrator, jarvis_client, _sub, _store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[_say_response(speech="ok")],
    )

    await orchestrator.process_user_message("s1", "hi")

    tools = jarvis_client.complete_calls[0]["tools"]
    assert tools is not None
    names = [t.name for t in tools]
    assert names == [
        "say",
        "show_task_result",
        "spawn_task",
        "addendum_task",
        "replan_task",
        "cancel_task",
    ]
    say_tool = tools[0]
    show_tool = tools[1]
    spawn_task_tool = tools[2]
    addendum_tool = tools[3]
    replan_tool = tools[4]
    cancel_task_tool = tools[5]
    assert say_tool.parameters["required"] == ["speech"]
    assert show_tool.parameters["required"] == ["speech", "query"]
    assert spawn_task_tool.parameters["required"] == ["title", "goal"]
    assert addendum_tool.parameters["required"] == ["task_id", "info"]
    assert replan_tool.parameters["required"] == ["task_id", "new_goal"]
    assert cancel_task_tool.parameters["required"] == ["task_id"]


# ---------------------------------------------------------------------------
# End-to-end: Jarvis spawns → SubAgentRunner completes → task=done + result.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WS event emission (slice #0019)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_emits_task_created_via_orchestrator() -> None:
    """On spawn the orchestrator emits the `task_created` event itself.

    The matching `task_updated` (pending → running) is the scheduler's
    responsibility — covered in :mod:`test_task_scheduler`. With a
    recording-scheduler double here, only the ``task_created`` event is
    emitted from the orchestrator path.
    """

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sc, _js, task_store, _scheduler = _make_orchestrator(
            complete_responses=[
                LLMResponse(
                    text=None,
                    tool_calls=[_spawn_tool_call(title="T", goal="G")],
                )
            ],
        )

        response = await orchestrator.process_user_message("s1", "hi")
        assert len(response.spawned_task_ids) == 1
        task_id = response.spawned_task_ids[0]
    finally:
        ws_events.set_emitter(None)

    task_events = [e for e in received if e["task_id"] == task_id]
    assert len(task_events) == 1
    created = task_events[0]
    assert created["type"] == "task_created"
    assert created["state"] == "spawned"
    assert created["title"] == "T"
    assert created["goal"] == "G"
    assert isinstance(created["created_at"], str)

    # The fake scheduler still transitions the task to running, mirroring
    # what the real one does when a slot is free.
    running = task_store.list_tasks(state="running")
    assert [t.id for t in running] == [task_id]


@pytest.mark.asyncio
async def test_spawn_with_invalid_args_does_not_emit_events() -> None:
    """When all tool calls are dropped, no task events are emitted.

    Issue 0048 retries the dispatch once then degrades through the
    hardcoded say; the spawn handler still never ran so the
    ``task_created`` WS event is not emitted.
    """

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        bad = ToolCall(
            id="call_bad",
            name="spawn_task",
            arguments={"title": "no goal"},
        )
        orchestrator, _jc, _sc, _js, _ts, _scheduler = _make_orchestrator(
            complete_responses=[
                LLMResponse(text=None, tool_calls=[bad]),
                LLMResponse(text=None, tool_calls=[bad]),
            ],
        )
        response = await orchestrator.process_user_message("s1", "x")
        assert response.speech == "Désolé, peux-tu reformuler ?"
    finally:
        ws_events.set_emitter(None)

    # No ``task_created`` event landed (the dispatch errored before the
    # handler ran). Note: a ``jarvis.route`` debug event still fires
    # via the dispatcher; those are not delivered through ws_events.
    task_events = [e for e in received if e.get("type") == "task_created"]
    assert task_events == []


@pytest.mark.asyncio
async def test_end_to_end_spawn_and_complete() -> None:
    """Acceptance criterion #6: mock LLM Jarvis spawn + mock sub-agent done."""

    # The sub-agent's LLM is wired separately from the Jarvis one so we can
    # control what it returns when SubAgentRunner calls it.
    jarvis_client = FakeLLMClient(
        complete_responses=[
            LLMResponse(
                text=None,
                tool_calls=[_spawn_tool_call(title="Drafts", goal="Draft 3 thanks emails")],
            )
        ]
    )
    subagent_client = FakeLLMClient(
        chat_responses=[json.dumps({"action": "done", "result": "Here are 3 drafts: A, B, C"})]
    )
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)

    def _runner_factory(tid: str) -> Any:
        runner = SubAgentRunner(subagent_client=subagent_client, task_store=task_store)
        return runner.run(tid)

    scheduler = TaskScheduler(task_store=task_store, cap=3, runner_factory=_runner_factory)
    orchestrator = Orchestrator(
        jarvis_client=jarvis_client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        task_scheduler=scheduler,
        jarvis_prompt=_TEST_JARVIS_PROMPT,
    )

    response = await orchestrator.process_user_message("s1", "Draft 3 thanks emails")
    assert response.spawned_task_ids
    task_id = response.spawned_task_ids[0]

    # Drain the background sub-agent task. We give it one full event-loop
    # turn — the fake LLM returns synchronously so this is enough.
    while True:
        task = task_store.get_task(task_id)
        if task.state in ("done", "failed"):
            break
        await asyncio.sleep(0)

    final = task_store.get_task(task_id)
    assert final.state == "done"
    assert final.result == "Here are 3 drafts: A, B, C"
    messages = task_store.get_task_messages(task_id)
    assert any(m.action == "done" for m in messages)



# ---------------------------------------------------------------------------
# generate_proactive_message (slices #0021/#0025)
#
# Slice #0025 wraps generation in a queue gated by user idleness. The
# rendering tests below invoke the internal ``_do_*`` methods directly so we
# don't have to drive the flusher loop; the queue + buffering behaviour gets
# its own dedicated set of tests further down.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_generate_ask_user_paraphrase_emits_assistant_msg_with_flag() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, jarvis_client, _sub, jarvis_store, task_store, _scheduler = (
            _make_orchestrator(chat_responses=["Tu veux un ton plutôt formel ou amical ?"])
        )
        # Seed a task with an ask_user message.
        task_id = task_store.create_task(title="Email manager", goal="Draft email")
        task_store.update_state(task_id, "running")
        task_store.append_message(
            task_id,
            role="assistant",
            content="Quel ton : formel ou amical ?",
            action="ask_user",
        )
        task_store.update_state(task_id, "waiting_input")

        await orchestrator._do_generate_ask_user_paraphrase(task_id)
    finally:
        ws_events.set_emitter(None)

    assert len(received) == 1
    event = received[0]
    assert event["type"] == "assistant_msg"
    assert event["proactive"] is True
    assert event["speech"] == "Tu veux un ton plutôt formel ou amical ?"
    assert event["ui"] == []
    assert isinstance(event["msg_id"], str) and len(event["msg_id"]) == 32

    # The paraphrase prompt referenced the task title + raw question.
    chat_messages = jarvis_client.chat_calls[0]["messages"]
    user_msg = chat_messages[-1]
    assert user_msg["role"] == "user"
    assert "Email manager" in user_msg["content"]
    assert "Quel ton : formel ou amical ?" in user_msg["content"]
    # Prompt instructs Jarvis to avoid the term "sub-agent" in the rephrased
    # question; the instruction itself is allowed to mention it.
    assert "Ne mentionne pas" in user_msg["content"]

    # JarvisStore appended the paraphrased text so the next user turn sees it.
    history = jarvis_store.history()
    assert history[-1] == {
        "role": "assistant",
        "content": "Tu veux un ton plutôt formel ou amical ?",
    }


@pytest.mark.asyncio
async def test_do_generate_ask_user_paraphrase_unknown_task_is_silent() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator()
        await orchestrator._do_generate_ask_user_paraphrase("missing")
    finally:
        ws_events.set_emitter(None)

    assert received == []


@pytest.mark.asyncio
async def test_generate_proactive_message_unknown_kind_does_not_enqueue() -> None:
    """Only ``ask_user`` and ``done`` are accepted; anything else is dropped."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator()
        await orchestrator.generate_proactive_message("anything", "progress")
    finally:
        ws_events.set_emitter(None)

    assert received == []
    assert orchestrator._proactive_queue.qsize() == 0


# ---------------------------------------------------------------------------
# generate_done_synthesis (slice #0025)
# ---------------------------------------------------------------------------


def _seed_done_task(task_store: TaskStore, *, title: str, result: str) -> str:
    """Helper: create a task in ``done`` state with ``result`` set."""

    task_id = task_store.create_task(title=title, goal="g")
    task_store.update_state(task_id, "running")
    task_store.set_result(task_id, result)
    task_store.update_state(task_id, "done")
    return task_id


@pytest.mark.asyncio
async def test_do_generate_done_synthesis_emits_assistant_msg_with_flag() -> None:
    """Direct invocation of ``_do_generate_done_synthesis`` emits the WS event."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, jarvis_client, _sub, jarvis_store, task_store, _scheduler = (
            _make_orchestrator(
                chat_responses=["La recherche est finie. En résumé : 3 papiers pertinents."],
            )
        )
        task_id = _seed_done_task(
            task_store, title="Recherche papier RAG", result="3 résultats trouvés"
        )

        await orchestrator._do_generate_done_synthesis(task_id)
    finally:
        ws_events.set_emitter(None)

    assert len(received) == 1
    event = received[0]
    assert event["type"] == "assistant_msg"
    assert event["proactive"] is True
    assert event["speech"] == "La recherche est finie. En résumé : 3 papiers pertinents."
    assert event["ui"] == []
    assert isinstance(event["msg_id"], str) and len(event["msg_id"]) == 32

    # Prompt referenced the task title + raw result text + the announcement
    # framing the orchestrator now mandates ("Voilà ce que j'ai trouvé …").
    chat_messages = jarvis_client.chat_calls[0]["messages"]
    user_msg = chat_messages[-1]
    assert user_msg["role"] == "user"
    assert "Recherche papier RAG" in user_msg["content"]
    assert "3 résultats trouvés" in user_msg["content"]
    # DONE_SYNTHESIS_TEMPLATE v2 — TTS-aware wording (read aloud, ~40 words,
    # 2 short sentences) replaced the old "2-3 lignes" phrasing.
    assert "2 phrases" in user_msg["content"]
    assert "Vérifie le contenu" in user_msg["content"]
    assert "Voilà ce que j'ai trouvé" in user_msg["content"]

    # Persisted in history so the user's next turn sees it.
    history = jarvis_store.history()
    assert history[-1] == {
        "role": "assistant",
        "content": "La recherche est finie. En résumé : 3 papiers pertinents.",
    }


@pytest.mark.asyncio
async def test_do_generate_done_synthesis_unknown_task_is_silent() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator()
        await orchestrator._do_generate_done_synthesis("missing")
    finally:
        ws_events.set_emitter(None)

    assert received == []


@pytest.mark.asyncio
async def test_do_generate_done_synthesis_handles_empty_result() -> None:
    """A task without a stored result still synthesises (empty string fed in)."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, task_store, _scheduler = _make_orchestrator(
            chat_responses=["C'est terminé."],
        )
        # No set_result → ``result`` is None on the row.
        task_id = task_store.create_task(title="T", goal="g")
        task_store.update_state(task_id, "running")
        task_store.update_state(task_id, "done")
        await orchestrator._do_generate_done_synthesis(task_id)
    finally:
        ws_events.set_emitter(None)

    assert len(received) == 1
    assert received[0]["speech"] == "C'est terminé."


# ---------------------------------------------------------------------------
# Proactive queue + buffer race conditions (slice #0025)
# ---------------------------------------------------------------------------


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    """Spin until ``predicate()`` is truthy, or return False after ``timeout``."""

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


@pytest.mark.asyncio
async def test_generate_proactive_message_enqueues_event() -> None:
    """``generate_proactive_message`` puts a tuple on the queue (no immediate emit)."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, task_store, _scheduler = _make_orchestrator()
        task_id = _seed_done_task(task_store, title="T", result="r")
        # Loop not started — the event sits in the queue.
        await orchestrator.generate_proactive_message(task_id, "done")
    finally:
        ws_events.set_emitter(None)

    assert received == []
    assert orchestrator._proactive_queue.qsize() == 1
    queued_id, queued_kind = await orchestrator._proactive_queue.get()
    assert queued_id == task_id
    assert queued_kind == "done"


@pytest.mark.asyncio
async def test_proactive_loop_buffers_while_thinking_then_flushes_when_idle() -> None:
    """Race condition: an event enqueued while ``_jarvis_state="thinking"`` is held."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, task_store, _scheduler = _make_orchestrator(
            chat_responses=["Synthèse OK."],
        )
        task_id = _seed_done_task(task_store, title="T", result="r")

        # Simulate Jarvis being mid-turn.
        orchestrator._jarvis_state = "thinking"
        orchestrator.start_proactive_loop()
        try:
            await orchestrator.generate_proactive_message(task_id, "done")

            # Give the flusher a couple of ticks; nothing should land yet
            # because state is still "thinking".
            await asyncio.sleep(0.15)
            assert received == []

            # Flip to idle → the flusher unblocks and emits.
            orchestrator._jarvis_state = "idle"
            flushed = await _wait_until(lambda: len(received) == 1)
            assert flushed
            assert received[0]["proactive"] is True
            assert received[0]["speech"] == "Synthèse OK."
        finally:
            await orchestrator.stop_proactive_loop()
    finally:
        ws_events.set_emitter(None)


@pytest.mark.asyncio
async def test_proactive_loop_respects_user_typing_then_flushes_on_reset() -> None:
    """User typing buffers the event; flush happens after ``set_user_typing(False)``."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, task_store, _scheduler = _make_orchestrator(
            chat_responses=["Synthèse OK."],
        )
        task_id = _seed_done_task(task_store, title="T", result="r")

        # The user is typing — buffer should hold.
        orchestrator.set_user_typing(True)
        orchestrator.start_proactive_loop()
        try:
            await orchestrator.generate_proactive_message(task_id, "done")
            await asyncio.sleep(0.15)
            assert received == []

            # Stop typing → flush within a couple of poll cycles.
            orchestrator.set_user_typing(False)
            flushed = await _wait_until(lambda: len(received) == 1)
            assert flushed
            assert received[0]["speech"] == "Synthèse OK."
        finally:
            await orchestrator.stop_proactive_loop()
    finally:
        ws_events.set_emitter(None)


@pytest.mark.asyncio
async def test_proactive_loop_flushes_fifo_when_idle() -> None:
    """Two enqueued events come out FIFO once the gate opens."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, task_store, _scheduler = _make_orchestrator(
            chat_responses=["First done.", "Second done."],
        )
        first = _seed_done_task(task_store, title="First", result="r1")
        second = _seed_done_task(task_store, title="Second", result="r2")

        orchestrator._jarvis_state = "thinking"
        orchestrator.start_proactive_loop()
        try:
            await orchestrator.generate_proactive_message(first, "done")
            await orchestrator.generate_proactive_message(second, "done")
            await asyncio.sleep(0.1)
            assert received == []

            orchestrator._jarvis_state = "idle"
            flushed = await _wait_until(lambda: len(received) == 2, timeout=2.0)
            assert flushed
            assert [e["speech"] for e in received] == ["First done.", "Second done."]
        finally:
            await orchestrator.stop_proactive_loop()
    finally:
        ws_events.set_emitter(None)


@pytest.mark.asyncio
async def test_set_user_typing_auto_resets_after_grace_window(monkeypatch: Any) -> None:
    """A True typing flag flips back to False on its own after the debounce."""

    from bob import orchestrator as orch_module

    # Shrink the grace window so the test stays fast.
    monkeypatch.setattr(orch_module, "_USER_TYPING_GRACE_S", 0.05)

    orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator()
    orchestrator.set_user_typing(True)
    assert orchestrator._user_typing is True
    await asyncio.sleep(0.15)
    assert orchestrator._user_typing is False


@pytest.mark.asyncio
async def test_process_user_message_sets_thinking_then_idle() -> None:
    """The user-turn entry point flips state to ``thinking`` and back to ``idle``."""

    orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator(
        complete_responses=[_say_response(speech="ok")],
    )
    assert orchestrator._jarvis_state == "idle"
    await orchestrator.process_user_message("s1", "hi")
    # Back to idle after the turn returns.
    assert orchestrator._jarvis_state == "idle"


@pytest.mark.asyncio
async def test_process_user_message_resets_state_on_exception() -> None:
    """Errors raised by the LLM still reset ``_jarvis_state`` via the finally clause."""

    orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator(
        complete_responses=[],  # FakeLLMClient.complete will raise AssertionError
    )
    with pytest.raises(AssertionError):
        await orchestrator.process_user_message("s1", "hi")
    assert orchestrator._jarvis_state == "idle"


# ---------------------------------------------------------------------------
# cancel_subtask tool dispatch (slice #0023)
# ---------------------------------------------------------------------------


def _cancel_tool_call(*, task_id: str, reason: str | None = None) -> ToolCall:
    args: dict[str, Any] = {"task_id": task_id}
    if reason is not None:
        args["reason"] = reason
    return ToolCall(
        id=f"call_{uuid4().hex[:6]}",
        name="cancel_task",
        arguments=args,
    )


@pytest.mark.asyncio
async def test_cancel_subtask_routes_to_scheduler_with_default_reason() -> None:
    """A ``cancel_subtask`` tool call lands on the scheduler with the default reason."""

    orchestrator, jarvis_client, _sub, jarvis_store, task_store, scheduler = _make_orchestrator()

    target_id = task_store.create_task(title="In flight", goal="Long task")
    task_store.update_state(target_id, "running")

    jarvis_client._complete_responses.append(
        LLMResponse(
            text=None,
            tool_calls=[_cancel_tool_call(task_id=target_id)],
        )
    )

    response = await orchestrator.process_user_message("s1", "annule la tâche")

    assert response.cancelled_task_ids == [target_id]
    assert response.spawned_task_ids == []
    assert response.forwarded_task_ids == []
    assert response.speech == "Compris, j'annule."

    # Reason defaults to "user_cancelled" when Jarvis omits it.
    assert scheduler.cancelled == [(target_id, "user_cancelled")]

    # Jarvis confirmation persisted in the singleton thread.
    history = jarvis_store.history()
    assert history[-1] == {"role": "assistant", "content": "Compris, j'annule."}


@pytest.mark.asyncio
async def test_cancel_subtask_forwards_custom_reason() -> None:
    """A custom ``reason`` arg is passed verbatim to the scheduler."""

    orchestrator, jarvis_client, _sub, _js, task_store, scheduler = _make_orchestrator()

    target_id = task_store.create_task(title="In flight", goal="Long task")
    task_store.update_state(target_id, "running")

    jarvis_client._complete_responses.append(
        LLMResponse(
            text=None,
            tool_calls=[_cancel_tool_call(task_id=target_id, reason="plus utile")],
        )
    )

    response = await orchestrator.process_user_message("s1", "laisse tomber")

    assert response.cancelled_task_ids == [target_id]
    assert scheduler.cancelled == [(target_id, "plus utile")]


@pytest.mark.asyncio
async def test_cancel_subtask_with_bad_task_id_degrades_after_retry() -> None:
    """Issue 0048: a malformed cancel retries once then degrades.

    The missing ``task_id`` field trips Pydantic validation; the retry
    budget gives the LLM one chance to recover, and then the
    orchestrator emits the hardcoded degrade speech through the
    dispatcher.
    """

    bad_call = ToolCall(
        id="call_bad",
        name="cancel_task",
        arguments={"reason": "nope"},
    )
    orchestrator, _jc, _sub, _js, _ts, scheduler = _make_orchestrator(
        complete_responses=[
            LLMResponse(text=None, tool_calls=[bad_call]),
            LLMResponse(text=None, tool_calls=[bad_call]),
        ],
    )

    response = await orchestrator.process_user_message("s1", "anything")

    assert response.speech == "Désolé, peux-tu reformuler ?"
    assert scheduler.cancelled == []


@pytest.mark.asyncio
async def test_cancel_subtask_empty_reason_falls_back_to_default() -> None:
    """An empty / whitespace reason still hands "user_cancelled" to the scheduler."""

    orchestrator, jarvis_client, _sub, _js, task_store, scheduler = _make_orchestrator()
    target_id = task_store.create_task(title="In flight", goal="Long task")
    task_store.update_state(target_id, "running")

    jarvis_client._complete_responses.append(
        LLMResponse(
            text=None,
            tool_calls=[_cancel_tool_call(task_id=target_id, reason="   ")],
        )
    )

    await orchestrator.process_user_message("s1", "annule")

    assert scheduler.cancelled == [(target_id, "user_cancelled")]


# ---------------------------------------------------------------------------
# Slice 0039 — debug event instrumentation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_user_message_emits_orchestrator_debug_milestones() -> None:
    """Acceptance criterion: input → decision → decision → output (milestones)."""

    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator(
        complete_responses=[_say_response(speech="Salut Tom")],
    )

    await orchestrator.process_user_message("sess-A", "Coucou")

    events = debug_log.snapshot()
    # FakeLLMClient does not emit llm_call_* events (only the real
    # LMStudio / Claude clients do). We assert the orchestrator-owned
    # milestone ordering here; the llm pair is tested via the real client
    # in :mod:`test_llm_complete`.
    categories = [(e.category, e.summary) for e in events]

    def _first_index(cat: str, prefix: str) -> int:
        for i, (c, s) in enumerate(categories):
            if c == cat and s.startswith(prefix):
                return i
        raise AssertionError(f"missing {cat}/{prefix} in {categories}")

    i_input = _first_index("input", "User envoie")
    i_thinking_start = _first_index("decision", "Jarvis réfléchit")
    i_thinking_end = _first_index("decision", "Jarvis a fini")
    i_output = _first_index("output", "Bob répond")

    assert i_input < i_thinking_start < i_thinking_end < i_output


@pytest.mark.asyncio
async def test_process_user_message_shares_turn_id_across_all_events() -> None:
    """Acceptance criterion: every event in a turn shares the same turn_id."""

    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator(
        complete_responses=[_say_response(speech="Salut")],
    )

    await orchestrator.process_user_message("sess-shared", "Hi")

    events = debug_log.snapshot()
    turn_ids = {e.turn_id for e in events if e.source.startswith("orchestrator")}
    # All orchestrator-source events should share the same turn_id and none
    # should be None.
    assert None not in turn_ids
    assert len(turn_ids) == 1


@pytest.mark.asyncio
async def test_two_consecutive_messages_produce_distinct_turn_ids() -> None:
    """Acceptance criterion: a new user_msg generates a new turn_id."""

    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator(
        complete_responses=[
            _say_response(speech="a"),
            _say_response(speech="b"),
        ],
    )

    await orchestrator.process_user_message("sess-X", "first")
    first_events = debug_log.snapshot()
    first_turn = next(e.turn_id for e in first_events if e.category == "input")

    await orchestrator.process_user_message("sess-X", "second")
    all_events = debug_log.snapshot()
    second_input = [
        e for e in all_events if e.category == "input" and e.summary.endswith('"second"')
    ]
    assert len(second_input) == 1
    second_turn = second_input[0].turn_id

    assert first_turn is not None
    assert second_turn is not None
    assert first_turn != second_turn


@pytest.mark.asyncio
async def test_spawn_subtask_emits_decision_debug_event() -> None:
    """The decision to spawn a sub-task shows up as a ``decision`` event."""

    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator(
        complete_responses=[
            LLMResponse(
                text=None,
                tool_calls=[_spawn_tool_call(title="Drafts", goal="Draft emails")],
            )
        ],
    )

    response = await orchestrator.process_user_message("sess-spawn", "draft 3")
    assert response.spawned_task_ids

    spawn_events = [
        e
        for e in debug_log.snapshot()
        if e.category == "decision" and "lance task v2" in e.summary
    ]
    assert len(spawn_events) == 1
    assert "Drafts" in spawn_events[0].summary
    payload = spawn_events[0].payload
    assert payload["title"] == "Drafts"
    assert payload["goal"] == "Draft emails"
    assert payload["task_id"] == response.spawned_task_ids[0]
    # All sibling events of the turn share the same turn_id.
    assert spawn_events[0].turn_id is not None


@pytest.mark.asyncio
async def test_subtask_runner_inherits_parent_turn_id() -> None:
    """Acceptance criterion: a sub-task spawned in a turn inherits its turn_id."""

    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    jarvis_client = FakeLLMClient(
        complete_responses=[
            LLMResponse(
                text=None,
                tool_calls=[_spawn_tool_call(title="Echo", goal="reply ok")],
            )
        ],
    )
    subagent_client = FakeLLMClient(chat_responses=[json.dumps({"action": "done", "result": "ok"})])
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)

    def _runner_factory(tid: str) -> Any:
        runner = SubAgentRunner(subagent_client=subagent_client, task_store=task_store)
        return runner.run(tid)

    scheduler = TaskScheduler(task_store=task_store, cap=3, runner_factory=_runner_factory)
    orchestrator = Orchestrator(
        jarvis_client=jarvis_client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        task_scheduler=scheduler,
        jarvis_prompt=_TEST_JARVIS_PROMPT,
    )

    response = await orchestrator.process_user_message("sess-inherit", "do it")
    task_id = response.spawned_task_ids[0]

    # Drain the background runner.
    while True:
        task = task_store.get_task(task_id)
        if task.state in ("done", "failed"):
            break
        await asyncio.sleep(0)

    parent_turn = next(
        e.turn_id for e in debug_log.snapshot() if e.category == "input" and "do it" in e.summary
    )
    assert parent_turn is not None

    # Every llm / task event emitted while the runner was alive must carry
    # the parent's turn_id thanks to ContextVar propagation through
    # asyncio.create_task.
    runner_events = [
        e
        for e in debug_log.snapshot()
        if e.source.startswith("bob.sub_agent_runner") or e.source.startswith("bob.task_scheduler")
    ]
    assert runner_events  # at least the _activate + _handle_done events
    assert all(e.turn_id == parent_turn for e in runner_events), [
        (e.source, e.summary, e.turn_id) for e in runner_events
    ]
