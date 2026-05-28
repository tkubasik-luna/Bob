"""Integration tests for the v2 :class:`bob.sub_agent.SubAgentRunner`.

PRD 0006 / issue 0045. Covers the acceptance criteria with scripted
fake LLM responses and a deterministic clock so the cap behaviours
are reproducible.

Tested termination paths:

- iteration cap → ``done(degraded, iteration_cap)``;
- wall-clock cap → ``done(timeout, wall_clock_cap)``;
- token cap → ``done(degraded, token_cap)``;
- cooperative cancel within grace → ``done(cancelled, user_cancelled)``;
- hard kill (CancelledError raised inside an LLM call) →
  ``done(cancelled, hard_killed)``.

The tests do NOT use ``asyncio.sleep`` to advance simulated time; the
injectable :data:`bob.sub_agent.runner.Clock` covers that.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent import (
    REASON_HARD_KILLED,
    REASON_ITERATION_CAP,
    REASON_TOKEN_CAP,
    REASON_USER_CANCELLED,
    REASON_WALL_CLOCK_CAP,
    SUB_AGENT_SCHEMA_VERSION,
    AddendumQueue,
    DoneAction,
    SubAgentActionParseError,
    SubAgentPolicy,
    SubAgentRunner,
    SubAgentToolDefinition,
    SubAgentToolHandlerOutcome,
    SubAgentToolRegistry,
    ToolCallAction,
    build_default_subagent_registry,
    parse_action,
)
from bob.sub_agent.tool_registry import (
    WebSearchArgs,
    build_web_fetch_tool,
    build_web_search_tool,
)
from bob.task_store import TaskStore


class _ScriptedClient(LLMClient):
    """Scripted ``chat()`` client; raises on missing scripted entries."""

    def __init__(
        self,
        *,
        chat_values: list[str] | None = None,
        chat_exc: BaseException | None = None,
        chat_callbacks: list[Any] | None = None,
    ) -> None:
        self._chat_values = list(chat_values or [])
        self._chat_exc = chat_exc
        # ``chat_callbacks`` are zero-arg sync callables run *before* the
        # corresponding chat() returns. Used to simulate cooperative
        # cancel midway through the run.
        self._chat_callbacks = list(chat_callbacks or [])
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.calls.append({"messages": messages, "schema": schema, "session_id": session_id})
        if self._chat_callbacks:
            cb = self._chat_callbacks.pop(0)
            if cb is not None:
                cb()
        if self._chat_exc is not None:
            raise self._chat_exc
        if not self._chat_values:
            raise AssertionError("_ScriptedClient ran out of canned chat() responses")
        return self._chat_values.pop(0)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError("not used in these tests")


class _ControllableClock:
    """Read/write monotonic-style clock for deterministic time tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _make_running_task(store: TaskStore, *, goal: str = "do the thing") -> str:
    task_id = store.create_task(title="t", goal=goal)
    store.update_state(task_id, "running")
    return task_id


def _progress_payload(thought: str) -> str:
    return json.dumps({"action": "progress", "thought": thought})


def _done_v2_payload(
    *,
    result_summary: str = "",
    status: str = "complete",
    reason_code: str = "ok",
    cost: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "action": "done",
            "result_summary": result_summary,
            "ui_payload": None,
            "status": status,
            "reason_code": reason_code,
            "cost": cost or {},
        }
    )


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------


def test_schema_version_is_one() -> None:
    """Acceptance: ``schema_version`` constant is 1."""

    assert SUB_AGENT_SCHEMA_VERSION == 1


def test_parse_done_v2_carries_all_fields() -> None:
    payload = {
        "action": "done",
        "result_summary": "OK",
        "ui_payload": {"component": "Markdown", "props": {"body": "X"}},
        "status": "complete",
        "reason_code": "ok",
        "cost": {"tokens": 42},
    }
    action = parse_action(payload)
    assert isinstance(action, DoneAction)
    assert action.result_summary == "OK"
    assert action.status == "complete"
    assert action.reason_code == "ok"
    assert action.cost == {"tokens": 42}
    assert action.ui_payload is not None
    assert action.ui_payload["component"] == "Markdown"
    assert action.schema_version == 1


def test_parse_progress_requires_thought() -> None:
    with pytest.raises(SubAgentActionParseError):
        parse_action({"action": "progress"})


def test_parse_tool_call_carries_args() -> None:
    action = parse_action({"action": "tool_call", "name": "web_search", "args": {"query": "x"}})
    assert isinstance(action, ToolCallAction)
    assert action.name == "web_search"
    assert action.args == {"query": "x"}


def test_parse_done_unknown_status_rejected() -> None:
    with pytest.raises(SubAgentActionParseError):
        parse_action(
            {
                "action": "done",
                "result_summary": "X",
                "status": "halted",
                "reason_code": "ok",
            }
        )


# ---------------------------------------------------------------------------
# Cap behaviours
# ---------------------------------------------------------------------------


async def _collect_state_changes(bus: EventBus) -> list[dict[str, Any]]:
    """Helper — capture ``task_state_changed`` payloads onto a list."""

    collected: list[dict[str, Any]] = []

    async def _on_change(payload: dict[str, Any]) -> None:
        collected.append(payload)

    bus.subscribe("task_state_changed", _on_change)
    return collected


@pytest.mark.asyncio
async def test_iteration_cap_emits_done_degraded() -> None:
    """Acceptance: iteration cap → forced ``done(degraded, iteration_cap)``."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    # 6 progress emits with cap=3 → the cap triggers BEFORE the 4th
    # progress is consumed: the runner's iteration counter has reached 3
    # at the top of the loop after 3 progress iterations were handled.
    client = _ScriptedClient(chat_values=[_progress_payload(f"step {i}") for i in range(1, 7)])
    policy = SubAgentPolicy(max_iterations=3, wall_clock_seconds=999.0, token_cap=10_000)
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=policy,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    task = store.get_task(task_id)
    assert task.state == "done"
    # task.result for cap paths is the empty summary (matches the v2
    # behaviour: cap → degraded, no payload produced).
    assert task.result == ""

    progress_msgs = [m for m in store.get_task_messages(task_id) if m.action == "progress"]
    assert len(progress_msgs) == 3
    assert len(client.calls) == 3  # the 4th iteration boundary tripped the cap

    # The bus carries the structured reason on the terminal state change.
    assert state_changes
    terminal = state_changes[-1]
    assert terminal["new_state"] == "done"
    assert terminal["status"] == "degraded"
    assert terminal["reason_code"] == REASON_ITERATION_CAP


@pytest.mark.asyncio
async def test_wall_clock_cap_emits_done_timeout() -> None:
    """Acceptance: wall-clock cap → forced ``done(timeout, wall_clock_cap)``."""

    store = _make_store()
    task_id = _make_running_task(store)
    clock = _ControllableClock()
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    # Advance the clock *inside* the chat callback so the next iteration
    # boundary's wall-clock check trips the cap.
    client = _ScriptedClient(
        chat_values=[
            _progress_payload("thinking"),
            _done_v2_payload(result_summary="ok"),
        ],
        chat_callbacks=[
            lambda: clock.advance(10.0),
            lambda: clock.advance(0.0),
        ],
    )
    policy = SubAgentPolicy(max_iterations=99, wall_clock_seconds=5.0, token_cap=10_000)
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=policy,
        clock=clock,
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    task = store.get_task(task_id)
    assert task.state == "failed"  # timeout maps to row-state failed
    # Only one chat call was made — the second iteration's wall-clock
    # check tripped before the next LLM call.
    assert len(client.calls) == 1
    system_rows = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    # The terminal reason is recorded on the system message body.
    assert system_rows
    assert system_rows[-1].content == REASON_WALL_CLOCK_CAP
    assert state_changes[-1]["status"] == "timeout"
    assert state_changes[-1]["reason_code"] == REASON_WALL_CLOCK_CAP


@pytest.mark.asyncio
async def test_token_cap_emits_done_degraded() -> None:
    """Acceptance: token cap → forced ``done(degraded, token_cap)``."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    # Tiny token cap (5) → the very first LLM round-trip exceeds it
    # because messages + raw response together estimate at > 5 chars/4.
    client = _ScriptedClient(chat_values=[_progress_payload("step 1"), _done_v2_payload()])
    policy = SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=5)
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=policy,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == ""
    # Token cap → degraded → done state. The reason rides on the bus.
    assert state_changes[-1]["status"] == "degraded"
    assert state_changes[-1]["reason_code"] == REASON_TOKEN_CAP


# ---------------------------------------------------------------------------
# Cancellation paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooperative_cancel_within_grace_emits_done_cancelled() -> None:
    """Acceptance: cooperative cancel within grace → ``done(cancelled, user_cancelled)``."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)

    # Build the runner up-front so the cooperative cancel callback can
    # close over it. The first chat() callback sets the flag; the
    # runner sees it at the next iteration boundary and exits cleanly.
    cancel_holder: dict[str, SubAgentRunner] = {}

    def _request_cancel() -> None:
        cancel_holder["runner"].request_cancel()

    client = _ScriptedClient(
        chat_values=[
            _progress_payload("thinking"),
            _done_v2_payload(result_summary="never reached"),
        ],
        chat_callbacks=[_request_cancel, None],
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=999_999),
        clock=_ControllableClock(),
    )
    cancel_holder["runner"] = runner

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    task = store.get_task(task_id)
    assert task.state == "failed"
    # The terminal reason is recorded on the system message body.
    system_rows = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    assert system_rows[-1].content == REASON_USER_CANCELLED
    # Only one chat call: the cancel landed at the next iteration boundary.
    assert len(client.calls) == 1
    assert state_changes[-1]["status"] == "cancelled"
    assert state_changes[-1]["reason_code"] == REASON_USER_CANCELLED


@pytest.mark.asyncio
async def test_hard_kill_emits_done_hard_killed() -> None:
    """Acceptance: cancel beyond grace → hard-kill, ``done(cancelled, hard_killed)``."""

    store = _make_store()
    task_id = _make_running_task(store)

    # Use a client whose chat() blocks on a never-set event. The runner
    # task is wrapped in ``asyncio.create_task`` then cancelled — the
    # runner must convert ``CancelledError`` into a terminal done with
    # ``hard_killed`` reason.
    block_forever: asyncio.Event = asyncio.Event()

    class _BlockingClient(LLMClient):
        async def chat(
            self,
            messages: list[dict[str, Any]],
            schema: dict[str, Any] | None = None,
            session_id: str | None = None,
        ) -> str:
            await block_forever.wait()
            return ""

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[ToolDefinition] | None = None,
            session_id: str | None = None,
        ) -> LLMResponse:
            raise NotImplementedError

    runner = SubAgentRunner(
        subagent_client=_BlockingClient(),
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        clock=_ControllableClock(),
    )

    task = asyncio.create_task(runner.run(task_id))
    # Let the runner reach its ``await client.chat`` point.
    for _ in range(3):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The runner persisted the terminal done before re-raising.
    persisted = store.get_task(task_id)
    assert persisted.state == "failed"
    system_rows = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    assert system_rows[-1].content == REASON_HARD_KILLED


# ---------------------------------------------------------------------------
# Addendum queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_addendum_drained_only_at_iteration_boundary() -> None:
    """Acceptance: addenda are drained at iteration boundaries only.

    The queue is populated AFTER the first chat() but BEFORE the second.
    We assert the next LLM message list contains the addendum, and
    nothing leaks into the first call.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    queue = AddendumQueue()

    client = _ScriptedClient(
        chat_values=[
            _progress_payload("step 1"),
            _done_v2_payload(result_summary="all good"),
        ],
        chat_callbacks=[
            # Producer fires between the first and second LLM calls —
            # which is exactly the iteration boundary we drain at.
            lambda: queue.put("Mid-flight info"),
            None,
        ],
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        addendum_queue=queue,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    # First call's messages must not contain the addendum.
    first_contents = [m["content"] for m in client.calls[0]["messages"]]
    assert not any("Mid-flight info" in c for c in first_contents)
    # Second call's messages must contain the addendum (folded into the
    # prompt by the iteration-boundary drain).
    second_contents = [m["content"] for m in client.calls[1]["messages"]]
    assert any("Mid-flight info" in c for c in second_contents)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def test_default_subagent_registry_exposes_gmail_search() -> None:
    """Issue 0055: ``gmail_search`` is the first real sub-agent tool wired in.

    ``web_search`` / ``web_fetch`` remain unwired until a real HTTP backend
    lands — their handlers still raise ``NotImplementedError``. The default
    registry now exposes ``gmail_search`` so research sub-tasks can answer
    email-lookup goals via the Mail overlay.
    """

    registry = build_default_subagent_registry()
    assert registry.names() == ["gmail_search"]
    # Scaffolding for the web tools remains available so a future slice can
    # re-register them without re-deriving the tool shape.
    web_search = build_web_search_tool()
    assert web_search.qualified_name == "v1.web_search"
    assert web_search.args_model is WebSearchArgs
    assert build_web_fetch_tool().name == "web_fetch"


def test_sub_agent_v2_prompt_documents_inbox_fallback() -> None:
    """Regression for the 2026-05-28 12:24 ``iteration_cap`` post-mortem.

    Task ``73146d06…`` ("Dernier mail reçu") looped 24 times because the LLM
    kept calling ``gmail_search`` with no filter and the validator rejected
    every attempt with ``error_code: invalid_args``. The prompt now MUST
    instruct the model to fall back to ``label="INBOX"`` (received) or
    ``label="SENT"`` (sent) when the goal is generic, so a single rejected
    call is impossible.
    """

    from bob.context.prompt_fragments import SUB_AGENT_V2_SYSTEM_PROMPT

    rendered = SUB_AGENT_V2_SYSTEM_PROMPT.render(goal="dummy")
    assert 'label="INBOX"' in rendered
    assert 'label="SENT"' in rendered
    # Must be framed as a fallback for the generic-goal branch, not as an
    # always-on default (the specific-filter happy path stays untouched).
    assert "Fallback" in rendered or "fallback" in rendered


def test_subagent_registry_disjoint_from_jarvis_registry() -> None:
    """Sub-agent registry is a SEPARATE instance from :mod:`bob.tools`."""

    # Importing the Jarvis registry must not influence the sub-agent one
    # — they are constructed independently.
    from bob.tools import build_default_registry as build_jarvis_registry

    jarvis = build_jarvis_registry()
    subagent = build_default_subagent_registry()
    jarvis_names = set(jarvis.names())
    subagent_names = set(subagent.names())
    # No name overlap.
    assert jarvis_names.isdisjoint(subagent_names)


@pytest.mark.asyncio
async def test_tool_call_dispatches_through_subagent_registry() -> None:
    """A ``tool_call`` action drives the sub-agent tool dispatcher."""

    store = _make_store()
    task_id = _make_running_task(store)

    # Custom registry with one stub tool so we don't trip on the
    # placeholder ``NotImplementedError`` raised by web_search / web_fetch.
    from pydantic import BaseModel

    class _NoopArgs(BaseModel):
        value: str

    async def _handler(_ctx: Any, args: BaseModel) -> SubAgentToolHandlerOutcome:
        assert isinstance(args, _NoopArgs)
        return SubAgentToolHandlerOutcome(status="ok", result={"echo": args.value})

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="noop",
                version="v1",
                description="stub",
                args_model=_NoopArgs,
                handler=_handler,
            )
        ]
    )

    client = _ScriptedClient(
        chat_values=[
            json.dumps({"action": "tool_call", "name": "noop", "args": {"value": "hi"}}),
            _done_v2_payload(result_summary="done"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    # The tool call + result both round-trip into the task log so the
    # next LLM iteration can read them.
    messages = store.get_task_messages(task_id)
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "noop" in tool_msgs[0].content
    assert "hi" in tool_msgs[0].content


# ---------------------------------------------------------------------------
# Cooperative cancellation at tool-call boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_between_tool_and_iteration_exits_cleanly() -> None:
    """Cancellation at the tool-call boundary also routes through ``done(cancelled)``."""

    store = _make_store()
    task_id = _make_running_task(store)

    # First we let the runner emit a tool_call; on the chat that would
    # produce the next iteration, set the cancel flag — the iteration
    # boundary checkpoint trips before consuming the response.
    cancel_holder: dict[str, SubAgentRunner] = {}

    def _request_cancel() -> None:
        cancel_holder["runner"].request_cancel()

    from pydantic import BaseModel

    class _NoopArgs(BaseModel):
        pass

    async def _handler(_ctx: Any, _args: BaseModel) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(status="ok", result={})

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="noop",
                version="v1",
                description="stub",
                args_model=_NoopArgs,
                handler=_handler,
            )
        ]
    )

    client = _ScriptedClient(
        chat_values=[
            json.dumps({"action": "tool_call", "name": "noop", "args": {}}),
            _done_v2_payload(result_summary="late"),
        ],
        chat_callbacks=[_request_cancel, None],
    )

    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )
    cancel_holder["runner"] = runner

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    system_rows = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    assert system_rows[-1].content == REASON_USER_CANCELLED
