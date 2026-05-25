"""Tests for :mod:`bob.sub_agent_runner`."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from bob import debug_log, ws_events
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
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
        chat_values: list[str] | None = None,
    ) -> None:
        self._chat_value = chat_value
        self._chat_values = list(chat_values or [])
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
        if self._chat_values:
            return self._chat_values.pop(0)
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
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())

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
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())

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
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    assert task.result is None
    messages = store.get_task_messages(task_id)
    assert any(m.role == "system" for m in messages)


@pytest.mark.asyncio
async def test_llm_exception_marks_failed_and_does_not_reraise() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_exc=RuntimeError("kaboom"))
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())

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
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    assert task.result is None


# ---------------------------------------------------------------------------
# WS event emission (slice #0019)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_action_emits_task_updated_and_task_result() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        client = _ScriptedClient(chat_value='{"action": "done", "result": "all good"}')
        runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())
        await runner.run(task_id)
    finally:
        ws_events.set_emitter(None)

    # Slice #0024 adds a leading ``task_message`` for the appended done entry.
    assert [e["type"] for e in received] == ["task_message", "task_updated", "task_result"]
    message_evt, updated, result = received

    assert message_evt["task_id"] == task_id
    assert message_evt["role"] == "assistant"
    assert message_evt["content"] == "all good"
    assert message_evt["action"] == "done"
    assert isinstance(message_evt["message_id"], int)

    assert updated["type"] == "task_updated"
    assert updated["task_id"] == task_id
    assert updated["state"] == "done"
    assert updated["needs_attention"] is False
    assert isinstance(updated["updated_at"], str)

    assert result == {
        "type": "task_result",
        "task_id": task_id,
        "result": "all good",
    }


@pytest.mark.asyncio
async def test_failure_path_emits_task_updated_failed_and_reason_result() -> None:
    """LLM exception → ``failed`` state with the reason surfaced as task_result."""

    store = _make_store()
    task_id = _make_running_task(store)

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        client = _ScriptedClient(chat_exc=RuntimeError("kaboom"))
        runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())
        await runner.run(task_id)
    finally:
        ws_events.set_emitter(None)

    # Slice #0024 adds a leading ``task_message`` for the appended system reason.
    assert [e["type"] for e in received] == ["task_message", "task_updated", "task_result"]
    message_evt, updated, result = received
    assert message_evt["role"] == "system"
    assert "kaboom" in message_evt["content"]
    assert updated["type"] == "task_updated"
    assert updated["state"] == "failed"
    assert result["type"] == "task_result"
    assert "kaboom" in result["result"]


# ---------------------------------------------------------------------------
# ask_user action (slice #0021) — multi-turn flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_action_transitions_to_waiting_input_and_persists_question() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_value='{"action": "ask_user", "question": "Quel ton ?"}')
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "waiting_input"
    assert task.result is None

    messages = store.get_task_messages(task_id)
    assert any(
        m.action == "ask_user" and m.role == "assistant" and m.content == "Quel ton ?"
        for m in messages
    )


@pytest.mark.asyncio
async def test_ask_user_action_emits_task_updated_and_bus_event() -> None:
    store = _make_store()
    task_id = _make_running_task(store)

    received_ws: list[dict[str, Any]] = []

    async def _ws_emitter(event: dict[str, Any]) -> None:
        received_ws.append(event)

    bus = EventBus()
    received_bus: list[dict[str, Any]] = []

    async def _bus_subscriber(payload: dict[str, Any]) -> None:
        received_bus.append(payload)

    bus.subscribe("task_state_changed", _bus_subscriber)

    ws_events.set_emitter(_ws_emitter)
    try:
        client = _ScriptedClient(
            chat_value='{"action": "ask_user", "question": "Formel ou amical ?"}'
        )
        runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=bus)
        await runner.run(task_id)
    finally:
        ws_events.set_emitter(None)

    # WS: task_message for the question + task_updated (no task_result on ask_user).
    assert [e["type"] for e in received_ws] == ["task_message", "task_updated"]
    assert received_ws[0]["role"] == "assistant"
    assert received_ws[0]["action"] == "ask_user"
    assert received_ws[0]["content"] == "Formel ou amical ?"
    assert received_ws[1]["state"] == "waiting_input"

    # Wait one event-loop tick so the bus' fire-and-forget subscriber runs.
    import asyncio

    for _ in range(3):
        await asyncio.sleep(0)
    assert len(received_bus) == 1
    payload = received_bus[0]
    assert payload["task_id"] == task_id
    assert payload["old_state"] == "running"
    assert payload["new_state"] == "waiting_input"
    assert payload["action"] == "ask_user"


@pytest.mark.asyncio
async def test_resume_after_forward_replays_history_and_completes() -> None:
    """Round-trip: first turn ask_user → forward user answer → second turn done.

    Simulates the orchestrator's forward_to_subtask behaviour by directly
    appending the user message + transitioning waiting_input → running, then
    re-running the runner with a fresh LLM canned response.
    """

    store = _make_store()
    task_id = store.create_task(title="t", goal="Draft email")
    store.update_state(task_id, "running")

    bus = EventBus()
    client = _ScriptedClient(
        chat_values=[
            '{"action": "ask_user", "question": "Formel ou amical ?"}',
            '{"action": "done", "result": "Email draft here"}',
        ]
    )
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=bus)

    await runner.run(task_id)
    assert store.get_task(task_id).state == "waiting_input"

    # Simulate the orchestrator's forward_to_subtask handoff.
    store.append_message(task_id, role="user", content="Amical.")
    store.update_state(task_id, "running")

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "Email draft here"

    # The second LLM call must have seen the prior ask_user turn AND the
    # forwarded user message in its history.
    assert len(client.calls) == 2
    second_call_msgs = client.calls[1]["messages"]
    contents = [m["content"] for m in second_call_msgs]
    assert "Formel ou amical ?" in contents
    assert "Amical." in contents


# ---------------------------------------------------------------------------
# progress action (slice #0022) — intermediate status loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_sequence_then_done_persists_messages_and_emits_events() -> None:
    """3 progress emits then done: state ends ``done``, all 4 messages logged."""

    store = _make_store()
    task_id = _make_running_task(store)

    received_ws: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received_ws.append(event)

    ws_events.set_emitter(_emitter)
    try:
        client = _ScriptedClient(
            chat_values=[
                '{"action": "progress", "status": "step 1"}',
                '{"action": "progress", "status": "step 2"}',
                '{"action": "progress", "status": "step 3"}',
                '{"action": "done", "result": "all good"}',
            ]
        )
        runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())
        await runner.run(task_id)
    finally:
        ws_events.set_emitter(None)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "all good"

    # 3 progress assistant messages + 1 done assistant message in the log.
    messages = store.get_task_messages(task_id)
    progress_msgs = [m for m in messages if m.action == "progress"]
    done_msgs = [m for m in messages if m.action == "done"]
    assert [m.content for m in progress_msgs] == ["step 1", "step 2", "step 3"]
    assert len(done_msgs) == 1
    assert done_msgs[0].content == "all good"
    assert all(m.role == "assistant" for m in progress_msgs + done_msgs)

    # WS event sequence: each progress emits (task_message, task_updated),
    # then done emits (task_message, task_updated, task_result).
    event_types = [e["type"] for e in received_ws]
    assert event_types == [
        "task_message",
        "task_updated",  # progress 1
        "task_message",
        "task_updated",  # progress 2
        "task_message",
        "task_updated",  # progress 3
        "task_message",
        "task_updated",  # done
        "task_result",
    ]
    # Each progress task_updated carries ``progress_status`` and keeps
    # ``state="running"``; the final ``done`` task_updated drops the field.
    progress_updates = [
        e for e in received_ws if e["type"] == "task_updated" and e["state"] == "running"
    ]
    assert [e["progress_status"] for e in progress_updates] == ["step 1", "step 2", "step 3"]

    done_update = next(
        e for e in received_ws if e["type"] == "task_updated" and e["state"] == "done"
    )
    assert "progress_status" not in done_update

    # The 4 LLM calls all received the same goal but each subsequent call
    # also saw the previously persisted progress entries in its history.
    assert len(client.calls) == 4
    fourth_call_msgs = client.calls[3]["messages"]
    contents = [m["content"] for m in fourth_call_msgs]
    assert "step 1" in contents
    assert "step 2" in contents
    assert "step 3" in contents


@pytest.mark.asyncio
async def test_progress_cap_exceeded_fails_with_max_iterations_reason() -> None:
    """11 consecutive progress emits → state ``failed`` with the cap reason."""

    store = _make_store()
    task_id = _make_running_task(store)

    received_ws: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received_ws.append(event)

    ws_events.set_emitter(_emitter)
    try:
        client = _ScriptedClient(
            chat_values=[f'{{"action": "progress", "status": "step {i}"}}' for i in range(1, 12)]
        )
        runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())
        await runner.run(task_id)
    finally:
        ws_events.set_emitter(None)

    task = store.get_task(task_id)
    assert task.state == "failed"
    # ``_fail`` only writes the reason on a system message + ``task_result``
    # event; ``task.result`` is reserved for ``done`` payloads.
    assert task.result is None

    # Only the first 10 progress emits made it through; the 11th tripped
    # the cap before persisting.
    messages = store.get_task_messages(task_id)
    progress_msgs = [m for m in messages if m.action == "progress"]
    assert len(progress_msgs) == 10
    # The system row carrying the failure reason is also present.
    system_msgs = [m for m in messages if m.role == "system"]
    assert any("max_iterations_exceeded" in m.content for m in system_msgs)

    # The final task_result event surfaces the cap reason verbatim.
    results = [e for e in received_ws if e["type"] == "task_result"]
    assert len(results) == 1
    assert results[0]["result"] == "max_iterations_exceeded"


@pytest.mark.asyncio
async def test_progress_does_not_trigger_proactivity_handler() -> None:
    """ProactivityHandler must stay silent on progress emits (no state change)."""

    store = _make_store()
    task_id = _make_running_task(store)

    bus = EventBus()
    state_change_payloads: list[dict[str, Any]] = []
    message_added_payloads: list[dict[str, Any]] = []

    async def _on_state_changed(payload: dict[str, Any]) -> None:
        state_change_payloads.append(payload)

    async def _on_message_added(payload: dict[str, Any]) -> None:
        message_added_payloads.append(payload)

    bus.subscribe("task_state_changed", _on_state_changed)
    bus.subscribe("task_message_added", _on_message_added)

    client = _ScriptedClient(
        chat_values=[
            '{"action": "progress", "status": "step 1"}',
            '{"action": "progress", "status": "step 2"}',
            '{"action": "done", "result": "ok"}',
        ]
    )
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=bus)
    await runner.run(task_id)

    # Let the bus' fire-and-forget subscriber tasks finish.
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)

    # Progress did NOT publish ``task_state_changed`` — only the final
    # ``done`` did (running → done).
    assert len(state_change_payloads) == 1
    assert state_change_payloads[0]["new_state"] == "done"
    assert state_change_payloads[0]["action"] == "done"

    # Both progress emits published on the ``task_message_added`` topic.
    progress_added = [p for p in message_added_payloads if p.get("action") == "progress"]
    assert len(progress_added) == 2
    assert all(p["role"] == "assistant" for p in progress_added)
    assert all(p["task_id"] == task_id for p in progress_added)


# ---------------------------------------------------------------------------
# Slice 0043 — parent_task_id propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_emits_debug_events_with_parent_task_id() -> None:
    """Every debug event emitted by the runner carries ``parent_task_id == task_id``.

    Covers the happy path: a single ``done`` action emits the ``_handle_done``
    debug event with the parent id set. The wrapper ``start_task`` /
    ``current_task_id.reset`` is symmetric so after ``run`` returns the
    ContextVar is restored to ``None``.
    """

    # Reset the ring buffer + ContextVar so this test sees only its emits.
    debug_log.clear()
    debug_log.current_task_id.set(None)

    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_value='{"action": "done", "result": "ok"}')
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())
    await runner.run(task_id)

    # After run returns, the wrapper restored the previous (None) value.
    assert debug_log.current_task_id.get() is None

    events = [e for e in debug_log.snapshot() if e.source.startswith("bob.sub_agent_runner")]
    assert len(events) >= 1
    assert all(e.parent_task_id == task_id for e in events)


@pytest.mark.asyncio
async def test_runner_failure_path_emits_debug_with_parent_task_id() -> None:
    """The ``_fail`` debug event also carries the parent task id."""

    debug_log.clear()
    debug_log.current_task_id.set(None)

    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_exc=RuntimeError("kaboom"))
    runner = SubAgentRunner(subagent_client=client, task_store=store, event_bus=EventBus())
    await runner.run(task_id)

    fail_events = [e for e in debug_log.snapshot() if e.source.endswith("._fail")]
    assert len(fail_events) == 1
    assert fail_events[0].parent_task_id == task_id
    # And the ContextVar is restored.
    assert debug_log.current_task_id.get() is None
