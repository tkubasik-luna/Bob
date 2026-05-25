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
from bob.orchestrator import _SPAWN_CONFIRMATION, Orchestrator
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
        name="spawn_subtask",
        arguments={"title": title, "goal": goal},
    )


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
async def test_spawn_with_invalid_args_falls_back_to_text() -> None:
    # Tool call with missing 'goal' → orchestrator drops it and re-routes to chat().
    bad_call = ToolCall(
        id="call_bad",
        name="spawn_subtask",
        arguments={"title": "no goal here"},
    )
    orchestrator, jarvis_client, _sub_client, _jarvis_store, task_store, scheduler = (
        _make_orchestrator(
            complete_responses=[LLMResponse(text=None, tool_calls=[bad_call])],
            chat_responses=[_valid_payload("Fallback reply")],
        )
    )

    response = await orchestrator.process_user_message("s1", "anything")

    assert response.speech == "Fallback reply"
    assert response.spawned_task_ids == []
    assert task_store.list_tasks() == []
    assert scheduler.enqueued == []
    assert len(jarvis_client.complete_calls) == 1
    assert len(jarvis_client.chat_calls) == 1


# ---------------------------------------------------------------------------
# Plain text path (no spawn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tool_call_routes_to_structured_chat() -> None:
    orchestrator, jarvis_client, _sub, jarvis_store, task_store, _scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text="just chatting", tool_calls=[])],
        chat_responses=[
            json.dumps(
                {
                    "speech": "Salut Tom",
                    "ui": [
                        {
                            "component": "Markdown",
                            "props": {"content": "**hi**"},
                        }
                    ],
                }
            )
        ],
    )

    response = await orchestrator.process_user_message("s1", "Coucou")

    assert response.speech == "Salut Tom"
    assert response.spawned_task_ids == []
    assert len(response.ui) == 1
    assert response.ui[0].component == "Markdown"

    assert task_store.list_tasks() == []
    assert jarvis_store.history() == [
        {"role": "user", "content": "Coucou"},
        {"role": "assistant", "content": "Salut Tom"},
    ]
    assert len(jarvis_client.chat_calls) == 1
    assert jarvis_client.chat_calls[0]["schema"] is not None


@pytest.mark.asyncio
async def test_no_tool_call_uses_jarvis_prompt_in_system_message() -> None:
    orchestrator, jarvis_client, _sub, _store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text="just chatting", tool_calls=[])],
        chat_responses=[_valid_payload("ok")],
    )

    await orchestrator.process_user_message("s1", "hi")

    # The complete() call carries the tools addendum; the chat() call should NOT.
    complete_system = jarvis_client.complete_calls[0]["messages"][0]
    chat_system = jarvis_client.chat_calls[0]["messages"][0]
    assert complete_system["role"] == "system"
    assert chat_system["role"] == "system"
    assert _TEST_JARVIS_PROMPT in complete_system["content"]
    assert _TEST_JARVIS_PROMPT in chat_system["content"]
    assert "spawn_subtask" in complete_system["content"]
    assert "spawn_subtask" not in chat_system["content"]


@pytest.mark.asyncio
async def test_parser_retry_does_not_pollute_jarvis_history() -> None:
    orchestrator, jarvis_client, _sub, jarvis_store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text="just chatting", tool_calls=[])],
        chat_responses=["not json at all", _valid_payload("retry-ok")],
    )

    response = await orchestrator.process_user_message("s1", "Ping")

    assert response.speech == "retry-ok"
    assert jarvis_store.history() == [
        {"role": "user", "content": "Ping"},
        {"role": "assistant", "content": "retry-ok"},
    ]
    assert len(jarvis_client.chat_calls) == 2


@pytest.mark.asyncio
async def test_fallback_when_both_chat_attempts_invalid() -> None:
    orchestrator, jarvis_client, _sub, jarvis_store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text="just chatting", tool_calls=[])],
        chat_responses=["garbage first", "garbage retry"],
    )

    response = await orchestrator.process_user_message("s1", "Question")

    assert response.speech == "garbage first"
    assert response.ui == []
    assert jarvis_store.history() == [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "garbage first"},
    ]
    assert len(jarvis_client.chat_calls) == 2


# ---------------------------------------------------------------------------
# spawn_subtask tool definition is what gets sent down to complete()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_call_sends_spawn_forward_and_cancel_tools() -> None:
    orchestrator, jarvis_client, _sub, _store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text="hello", tool_calls=[])],
        chat_responses=[_valid_payload("ok")],
    )

    await orchestrator.process_user_message("s1", "hi")

    tools = jarvis_client.complete_calls[0]["tools"]
    assert tools is not None
    names = [t.name for t in tools]
    assert names == ["spawn_subtask", "forward_to_subtask", "cancel_subtask"]
    spawn_tool = tools[0]
    forward_tool = tools[1]
    cancel_tool = tools[2]
    assert spawn_tool.parameters["required"] == ["title", "goal"]
    assert forward_tool.parameters["required"] == ["task_id", "response"]
    assert cancel_tool.parameters["required"] == ["task_id"]


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
    assert created["state"] == "pending"
    assert created["title"] == "T"
    assert created["goal"] == "G"
    assert isinstance(created["created_at"], str)

    # The fake scheduler still transitions the task to running, mirroring
    # what the real one does when a slot is free.
    running = task_store.list_tasks(state="running")
    assert [t.id for t in running] == [task_id]


@pytest.mark.asyncio
async def test_spawn_with_invalid_args_does_not_emit_events() -> None:
    """When all tool calls are dropped, no task events are emitted."""

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        bad = ToolCall(
            id="call_bad",
            name="spawn_subtask",
            arguments={"title": "no goal"},
        )
        orchestrator, _jc, _sc, _js, _ts, _scheduler = _make_orchestrator(
            complete_responses=[LLMResponse(text=None, tool_calls=[bad])],
            chat_responses=[_valid_payload("fallback")],
        )
        response = await orchestrator.process_user_message("s1", "x")
        assert response.spawned_task_ids == []
    finally:
        ws_events.set_emitter(None)

    assert received == []


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
# forward_to_subtask tool dispatch (slice #0021)
# ---------------------------------------------------------------------------


def _forward_tool_call(*, task_id: str, response: str = "Amical.") -> ToolCall:
    return ToolCall(
        id=f"call_{uuid4().hex[:6]}",
        name="forward_to_subtask",
        arguments={"task_id": task_id, "response": response},
    )


@pytest.mark.asyncio
async def test_forward_to_subtask_inserts_message_and_calls_resume() -> None:
    """A ``forward_to_subtask`` tool call must append the user message + resume."""

    orchestrator, jarvis_client, _sub, jarvis_store, task_store, scheduler = _make_orchestrator()

    # Seed a task in waiting_input with a prior ask_user question.
    target_id = task_store.create_task(title="Draft email", goal="Write a draft")
    task_store.update_state(target_id, "running")
    task_store.append_message(target_id, role="assistant", content="Quel ton ?", action="ask_user")
    task_store.update_state(target_id, "waiting_input")

    # Now feed the orchestrator a turn whose only tool call is a forward.
    jarvis_client._complete_responses.append(
        LLMResponse(
            text=None,
            tool_calls=[_forward_tool_call(task_id=target_id, response="Amical.")],
        )
    )

    response = await orchestrator.process_user_message("s1", "Amical.")

    assert response.forwarded_task_ids == [target_id]
    assert response.spawned_task_ids == []
    assert "transmets" in response.speech

    assert scheduler.resumed == [target_id]
    # Recording-scheduler transitions the task to running on resume.
    assert task_store.get_task(target_id).state == "running"

    # The user's reply was persisted as a ``user`` row on the task log.
    forwarded_msg = [m for m in task_store.get_task_messages(target_id) if m.role == "user"]
    assert [m.content for m in forwarded_msg] == ["Amical."]

    # The orchestrator must have advertised the waiting_input task in its
    # system prompt so Jarvis knows the task_id to forward to.
    system_content = jarvis_client.complete_calls[-1]["messages"][0]["content"]
    assert target_id in system_content
    assert "Quel ton ?" in system_content

    # Jarvis confirmation persisted in history.
    history = jarvis_store.history()
    assert history[-1]["role"] == "assistant"
    assert "transmets" in history[-1]["content"]


@pytest.mark.asyncio
async def test_forward_to_subtask_emits_task_message_event() -> None:
    """The forwarded user reply surfaces as a ``task_message`` WS event."""

    orchestrator, jarvis_client, _sub, _js, task_store, _scheduler = _make_orchestrator()

    target_id = task_store.create_task(title="Draft email", goal="Write a draft")
    task_store.update_state(target_id, "running")
    task_store.append_message(target_id, role="assistant", content="Quel ton ?", action="ask_user")
    task_store.update_state(target_id, "waiting_input")

    jarvis_client._complete_responses.append(
        LLMResponse(
            text=None,
            tool_calls=[_forward_tool_call(task_id=target_id, response="Amical.")],
        )
    )

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        await orchestrator.process_user_message("s1", "Amical.")
    finally:
        ws_events.set_emitter(None)

    task_messages = [e for e in received if e["type"] == "task_message"]
    assert len(task_messages) == 1
    evt = task_messages[0]
    assert evt["task_id"] == target_id
    assert evt["role"] == "user"
    assert evt["content"] == "Amical."
    assert evt["action"] is None
    assert isinstance(evt["message_id"], int)


@pytest.mark.asyncio
async def test_forward_to_unknown_task_falls_back_to_text() -> None:
    """A forward to a missing task is dropped — orchestrator replies in text."""

    bad_call = ToolCall(
        id="call_bad",
        name="forward_to_subtask",
        arguments={"task_id": "does-not-exist", "response": "x"},
    )
    orchestrator, _jarvis_client, _sub, _js, task_store, scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text=None, tool_calls=[bad_call])],
        chat_responses=[_valid_payload("fallback")],
    )

    response = await orchestrator.process_user_message("s1", "Amical.")

    assert response.speech == "fallback"
    assert response.forwarded_task_ids == []
    assert scheduler.resumed == []
    assert task_store.list_tasks() == []


@pytest.mark.asyncio
async def test_forward_to_task_not_in_waiting_input_is_dropped() -> None:
    """Forward must target a task in ``waiting_input``; running rejects it."""

    orchestrator, jarvis_client, _sub, _js, task_store, scheduler = _make_orchestrator(
        chat_responses=[_valid_payload("fallback")],
    )
    target_id = task_store.create_task(title="t", goal="g")
    task_store.update_state(target_id, "running")  # Not waiting_input!

    jarvis_client._complete_responses.append(
        LLMResponse(
            text=None,
            tool_calls=[_forward_tool_call(task_id=target_id, response="x")],
        )
    )

    response = await orchestrator.process_user_message("s1", "x")

    assert response.forwarded_task_ids == []
    assert response.speech == "fallback"
    assert scheduler.resumed == []
    # Original task untouched.
    assert task_store.get_task(target_id).state == "running"


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
    assert "2-3 lignes" in user_msg["content"]
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
        complete_responses=[LLMResponse(text="just chatting", tool_calls=[])],
        chat_responses=[_valid_payload("ok")],
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
        name="cancel_subtask",
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
async def test_cancel_subtask_with_bad_task_id_falls_back_to_text() -> None:
    """A malformed tool call (missing task_id) is dropped; text reply wins."""

    bad_call = ToolCall(
        id="call_bad",
        name="cancel_subtask",
        arguments={"reason": "nope"},
    )
    orchestrator, _jc, _sub, _js, _ts, scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text=None, tool_calls=[bad_call])],
        chat_responses=[_valid_payload("fallback")],
    )

    response = await orchestrator.process_user_message("s1", "anything")

    assert response.speech == "fallback"
    assert response.cancelled_task_ids == []
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
