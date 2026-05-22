"""Tests for :mod:`bob.orchestrator`."""

from __future__ import annotations

import asyncio
import json
import sqlite3
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

    Records each ``enqueue`` / ``resume`` call so tests can assert dispatch
    without spinning up real runner tasks. The fake transitions the task to
    ``running`` so existing assertions (``list_tasks(state="running")``)
    keep their meaning — that is the contract the real scheduler honours
    when a slot is free.
    """

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store
        self.enqueued: list[str] = []
        self.resumed: list[str] = []

    async def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)
        self._task_store.update_state(task_id, "running")

    async def resume(self, task_id: str) -> None:
        self.resumed.append(task_id)
        self._task_store.update_state(task_id, "running")


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
async def test_complete_call_sends_spawn_subtask_and_forward_tools() -> None:
    orchestrator, jarvis_client, _sub, _store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text="hello", tool_calls=[])],
        chat_responses=[_valid_payload("ok")],
    )

    await orchestrator.process_user_message("s1", "hi")

    tools = jarvis_client.complete_calls[0]["tools"]
    assert tools is not None
    names = [t.name for t in tools]
    assert names == ["spawn_subtask", "forward_to_subtask"]
    spawn_tool = tools[0]
    forward_tool = tools[1]
    assert spawn_tool.parameters["required"] == ["title", "goal"]
    assert forward_tool.parameters["required"] == ["task_id", "response"]


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
# generate_proactive_message (slice #0021)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_proactive_message_emits_assistant_msg_with_flag() -> None:
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

        await orchestrator.generate_proactive_message(task_id, "ask_user")
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
async def test_generate_proactive_message_unknown_task_is_silent() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, _ts, _scheduler = _make_orchestrator()
        await orchestrator.generate_proactive_message("missing", "ask_user")
    finally:
        ws_events.set_emitter(None)

    assert received == []


@pytest.mark.asyncio
async def test_generate_proactive_message_non_ask_user_kind_noops() -> None:
    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        orchestrator, _jc, _sub, _js, task_store, _scheduler = _make_orchestrator()
        task_id = task_store.create_task(title="t", goal="g")
        task_store.update_state(task_id, "running")
        task_store.set_result(task_id, "r")
        task_store.update_state(task_id, "done")
        await orchestrator.generate_proactive_message(task_id, "done")
    finally:
        ws_events.set_emitter(None)

    assert received == []
