"""Tests for :mod:`bob.sub_agent_runner`."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent_runner import SubAgentRunner
from bob.task_store import TaskStore


class _ScriptedClient(LLMClient):
    """LLMClient that returns / raises pre-canned values from ``chat()``."""

    def __init__(
        self,
        *,
        chat_value: str | None = None,
        chat_exc: BaseException | None = None,
    ) -> None:
        self._chat_value = chat_value
        self._chat_exc = chat_exc
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.calls.append({"messages": messages, "schema": schema, "session_id": session_id})
        if self._chat_exc is not None:
            raise self._chat_exc
        assert self._chat_value is not None
        return self._chat_value

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError("not used in these tests")


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _make_running_task(store: TaskStore, *, goal: str = "do the thing") -> str:
    task_id = store.create_task(title="t", goal=goal)
    store.update_state(task_id, "running")
    return task_id


# ---------------------------------------------------------------------------
# done action — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_action_persists_result_and_transitions_to_done() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_value='{"action": "done", "result": "ok"}')
    runner = SubAgentRunner(subagent_client=client, task_store=store)

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "ok"

    messages = store.get_task_messages(task_id)
    assert any(m.action == "done" and m.content == "ok" for m in messages)
    assert len(client.calls) == 1
    # System prompt + user goal forwarded.
    sent = client.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "do the thing" in sent[0]["content"]
    assert sent[1] == {"role": "user", "content": "do the thing"}


@pytest.mark.asyncio
async def test_done_action_fenced_json_parses() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    fenced = '```json\n{"action":"done","result":"X"}\n```'
    client = _ScriptedClient(chat_value=fenced)
    runner = SubAgentRunner(subagent_client=client, task_store=store)

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "X"


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_marks_failed_no_result() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_value="not json")
    runner = SubAgentRunner(subagent_client=client, task_store=store)

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    assert task.result is None
    messages = store.get_task_messages(task_id)
    assert any(m.role == "system" for m in messages)


@pytest.mark.asyncio
async def test_unsupported_ask_user_action_marks_failed_and_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_value='{"action": "ask_user", "question": "?"}')
    runner = SubAgentRunner(subagent_client=client, task_store=store)

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    assert task.result is None
    # structlog renders to stdout via ``PrintLoggerFactory``. The event name
    # + warning level are present whether the JSON renderer is configured
    # (when the FastAPI lifespan ran) or the default key-value renderer is in
    # effect (isolated unit test).
    out = capsys.readouterr().out
    assert "sub_agent_runner.unsupported_action" in out
    assert "warning" in out


@pytest.mark.asyncio
async def test_unsupported_progress_action_marks_failed() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_value='{"action": "progress", "status": "halfway"}')
    runner = SubAgentRunner(subagent_client=client, task_store=store)

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    assert task.result is None


@pytest.mark.asyncio
async def test_llm_exception_marks_failed_and_does_not_reraise() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_exc=RuntimeError("kaboom"))
    runner = SubAgentRunner(subagent_client=client, task_store=store)

    # Should NOT raise.
    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    assert task.result is None
    messages = store.get_task_messages(task_id)
    assert any("kaboom" in m.content for m in messages)


@pytest.mark.asyncio
async def test_done_action_without_result_marks_failed() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_value='{"action": "done"}')
    runner = SubAgentRunner(subagent_client=client, task_store=store)

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    assert task.result is None
