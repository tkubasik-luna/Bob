"""Tests for :mod:`bob.orchestrator`."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any
from uuid import uuid4

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import LLMClient
from bob.orchestrator import _SPAWN_CONFIRMATION, Orchestrator
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


def _make_orchestrator(
    *,
    complete_responses: list[LLMResponse] | None = None,
    chat_responses: list[str] | None = None,
    runner_factory: Any = None,
) -> tuple[Orchestrator, FakeLLMClient, FakeLLMClient, JarvisStore, TaskStore]:
    jarvis_client = FakeLLMClient(
        complete_responses=complete_responses,
        chat_responses=chat_responses,
    )
    subagent_client = FakeLLMClient()
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    orchestrator = Orchestrator(
        jarvis_client=jarvis_client,
        subagent_client=subagent_client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        jarvis_prompt=_TEST_JARVIS_PROMPT,
        sub_agent_runner_factory=runner_factory,
    )
    return orchestrator, jarvis_client, subagent_client, jarvis_store, task_store


def _spawn_tool_call(*, title: str = "Buy milk", goal: str = "Acheter du lait") -> ToolCall:
    return ToolCall(
        id=f"call_{uuid4().hex[:6]}",
        name="spawn_subtask",
        arguments={"title": title, "goal": goal},
    )


def _valid_payload(speech: str = "Bonjour Tom") -> str:
    return json.dumps({"speech": speech, "ui": []})


def _noop_runner_factory(_task_id: str) -> asyncio.Task[None]:
    async def _noop() -> None:
        return None

    return asyncio.create_task(_noop())


# ---------------------------------------------------------------------------
# Spawn path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_user_message_spawns_subtask_when_tool_call() -> None:
    spawned_task_ids: list[str] = []

    def runner_factory(task_id: str) -> asyncio.Task[None]:
        spawned_task_ids.append(task_id)
        return _noop_runner_factory(task_id)

    orchestrator, jarvis_client, _sub_client, jarvis_store, task_store = _make_orchestrator(
        complete_responses=[
            LLMResponse(
                text=None,
                tool_calls=[_spawn_tool_call(title="Drafts", goal="Draft 3 thanks emails")],
            )
        ],
        runner_factory=runner_factory,
    )

    response = await orchestrator.process_user_message("s1", "Draft 3 thanks emails")

    assert response.speech == _SPAWN_CONFIRMATION
    assert response.ui == []
    assert len(response.spawned_task_ids) == 1
    assert response.spawned_task_ids == spawned_task_ids

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
    orchestrator, jarvis_client, _sub_client, _jarvis_store, task_store = _make_orchestrator(
        complete_responses=[LLMResponse(text=None, tool_calls=[bad_call])],
        chat_responses=[_valid_payload("Fallback reply")],
        runner_factory=_noop_runner_factory,
    )

    response = await orchestrator.process_user_message("s1", "anything")

    assert response.speech == "Fallback reply"
    assert response.spawned_task_ids == []
    assert task_store.list_tasks() == []
    assert len(jarvis_client.complete_calls) == 1
    assert len(jarvis_client.chat_calls) == 1


# ---------------------------------------------------------------------------
# Plain text path (no spawn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tool_call_routes_to_structured_chat() -> None:
    orchestrator, jarvis_client, _sub, jarvis_store, task_store = _make_orchestrator(
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
    orchestrator, jarvis_client, _sub, _store, _ts = _make_orchestrator(
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
    orchestrator, jarvis_client, _sub, jarvis_store, _ts = _make_orchestrator(
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
    orchestrator, jarvis_client, _sub, jarvis_store, _ts = _make_orchestrator(
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
    orchestrator, jarvis_client, _sub, _store, _ts = _make_orchestrator(
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

    # Default factory wires a real SubAgentRunner around ``subagent_client``.
    orchestrator = Orchestrator(
        jarvis_client=jarvis_client,
        subagent_client=subagent_client,
        jarvis_store=jarvis_store,
        task_store=task_store,
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
