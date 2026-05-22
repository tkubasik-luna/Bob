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

    Records each ``enqueue(task_id)`` call so tests can assert dispatch
    without spinning up real runner tasks. The fake transitions the task to
    ``running`` so existing assertions (``list_tasks(state="running")``)
    keep their meaning — that is the contract the real scheduler honours
    when a slot is free.
    """

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store
        self.enqueued: list[str] = []

    async def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)
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
async def test_complete_call_sends_spawn_subtask_tool() -> None:
    orchestrator, jarvis_client, _sub, _store, _ts, _scheduler = _make_orchestrator(
        complete_responses=[LLMResponse(text="hello", tool_calls=[])],
        chat_responses=[_valid_payload("ok")],
    )

    await orchestrator.process_user_message("s1", "hi")

    tools = jarvis_client.complete_calls[0]["tools"]
    assert tools is not None
    assert [t.name for t in tools] == ["spawn_subtask"]
    assert tools[0].parameters["required"] == ["title", "goal"]


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
