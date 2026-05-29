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
    GmailSearchArgs,
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
from bob.sub_agent.actions import (
    SUB_AGENT_ACTION_SCHEMA_NAME,
    sub_agent_action_response_schema,
)
from bob.sub_agent.runner import _normalise_payload
from bob.sub_agent.tool_registry import (
    WebSearchArgs,
    build_gmail_search_tool,
    build_web_fetch_tool,
    build_web_search_tool,
)
from bob.task_store import TaskStore

from .fixtures.tool_calling import ENVELOPE_FIXTURES, EnvelopeFixture


class _ScriptedClient(LLMClient):
    """Scripted ``chat()`` client; raises on missing scripted entries."""

    def __init__(
        self,
        *,
        chat_values: list[str] | None = None,
        chat_exc: BaseException | None = None,
        chat_callbacks: list[Any] | None = None,
        guided: bool = False,
    ) -> None:
        self._chat_values = list(chat_values or [])
        self._chat_exc = chat_exc
        # ``chat_callbacks`` are zero-arg sync callables run *before* the
        # corresponding chat() returns. Used to simulate cooperative
        # cancel midway through the run.
        self._chat_callbacks = list(chat_callbacks or [])
        # Issue 0060 — when True the client advertises guided-JSON support so
        # the runner passes the ``SubAgentAction`` schema as ``response_format``
        # on each ``chat`` call. Mirrors :class:`bob.llm_client.LMStudioClient`.
        self._guided = guided
        self.calls: list[dict[str, Any]] = []

    def supports_guided_json(self) -> bool:
        return self._guided

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


def test_subagent_tool_to_spec_derives_parameters_from_args_model() -> None:
    """Issue 0059: ``SubAgentToolDefinition.to_spec()`` routes through ToolSpec.

    The spec's ``parameters`` is the ``args_model`` JSON Schema verbatim (no
    flattening at this slice — that is 0063) and the spec retains the model so
    a later self-correction phase can re-validate against it. Name/description
    pass through unchanged.
    """

    definition = build_gmail_search_tool()
    spec = definition.to_spec()

    assert spec.name == "gmail_search"
    assert spec.description == definition.description
    assert spec.args_model is GmailSearchArgs
    # Parameters derived verbatim from the Pydantic model (0058 contract).
    assert spec.parameters == GmailSearchArgs.model_json_schema()
    assert spec.parameters["type"] == "object"
    # Every structured filter field is present in the derived schema.
    assert set(spec.parameters["properties"]) == set(GmailSearchArgs.model_fields)


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
# Tool argument schema injection + pre-dispatch validation (issue 0059)
# ---------------------------------------------------------------------------


def test_validate_tool_args_is_the_predispatch_seam() -> None:
    """Issue 0059: ``_validate_tool_args`` is the structured pre-dispatch gate.

    White-box lock on the seam the self-correction loop (0062) will hook:

    - valid args → ``None`` (caller proceeds to dispatch);
    - unknown tool → structured ``unknown_tool`` (never reaches dispatch);
    - schema-invalid args → structured ``invalid_args`` naming the field.

    Exercising the method directly (not via a full run) proves the validation
    happens BEFORE dispatch — independent of the dispatcher's own re-validation.
    """

    from pydantic import BaseModel

    class _StrictArgs(BaseModel):
        value: str

    async def _handler(_ctx: Any, _args: BaseModel) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(status="ok", result={})

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="strict",
                version="v1",
                description="needs value",
                args_model=_StrictArgs,
                handler=_handler,
            )
        ]
    )
    runner = SubAgentRunner(
        subagent_client=_ScriptedClient(chat_values=[]),
        task_store=_make_store(),
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    # Valid → None (dispatch proceeds).
    assert (
        runner._validate_tool_args(
            ToolCallAction(action="tool_call", name="strict", args={"value": "ok"})
        )
        is None
    )

    # Unknown tool → structured unknown_tool error.
    unknown = runner._validate_tool_args(ToolCallAction(action="tool_call", name="ghost", args={}))
    assert unknown is not None
    assert unknown.outcome == "error"
    assert unknown.error_code == "unknown_tool"

    # Schema violation → structured invalid_args error naming the field.
    invalid = runner._validate_tool_args(ToolCallAction(action="tool_call", name="strict", args={}))
    assert invalid is not None
    assert invalid.outcome == "error"
    assert invalid.error_code == "invalid_args"
    assert invalid.tool_name == "strict"
    assert invalid.tool_version == "v1"
    assert "value" in (invalid.error_message or "")


@pytest.mark.asyncio
async def test_prompt_injects_tool_arg_schema() -> None:
    """Issue 0059: the system prompt advertises each tool's argument JSON Schema.

    Replaces the former name+description-only listing — the model used to
    guess argument names from the prose recipe. The system message now MUST
    carry the real ``gmail_search`` argument schema (every field name + a JSON
    Schema marker) so the model fills ``args`` from the schema, not a guess.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_values=[_done_v2_payload(result_summary="done")])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        tool_registry=build_default_subagent_registry(),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    system_prompt = client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["messages"][0]["role"] == "system"
    # Section header + a JSON-Schema fence (not the old "- name : desc" only).
    assert "Outils disponibles" in system_prompt
    assert "JSON Schema" in system_prompt
    assert '"type": "object"' in system_prompt
    # Every real gmail_search field name is now visible to the model.
    for field_name in GmailSearchArgs.model_fields:
        assert field_name in system_prompt
    # Constraints survive too (max_results is clamped 1..5).
    assert '"maximum": 5' in system_prompt


@pytest.mark.asyncio
async def test_tool_call_invalid_args_produces_structured_error_without_dispatch() -> None:
    """Issue 0059: args violating the schema short-circuit to a structured error.

    The handler MUST NOT run with a malformed payload (no blind dispatch); the
    failure round-trips to the LLM as a ``tool`` message with
    ``error_code="invalid_args"`` (no silent drop). Here ``value`` is required
    but omitted — pre-dispatch validation rejects it before the handler.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    from pydantic import BaseModel

    class _StrictArgs(BaseModel):
        value: str

    handler_calls: list[BaseModel] = []

    async def _handler(_ctx: Any, args: BaseModel) -> SubAgentToolHandlerOutcome:
        handler_calls.append(args)
        return SubAgentToolHandlerOutcome(status="ok", result={})

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="strict",
                version="v1",
                description="needs value",
                args_model=_StrictArgs,
                handler=_handler,
            )
        ]
    )

    client = _ScriptedClient(
        chat_values=[
            # ``value`` missing → schema violation.
            json.dumps({"action": "tool_call", "name": "strict", "args": {}}),
            _done_v2_payload(
                result_summary="gave up", status="failed", reason_code="invalid_output"
            ),
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

    # Handler never saw the malformed payload.
    assert handler_calls == []

    # A structured error tool-message round-tripped to the LLM (no silent drop).
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = json.loads(tool_msgs[0].content)
    assert body["tool"] == "strict"
    assert body["status"] == "error"
    assert body["error_code"] == "invalid_args"
    # The message names the offending field so the model can self-correct.
    assert "value" in body["error_message"]


@pytest.mark.asyncio
async def test_tool_call_unknown_tool_produces_structured_error() -> None:
    """Issue 0059: an unknown tool name surfaces a structured ``unknown_tool``.

    Validation resolves the tool by name BEFORE dispatch; a name not in the
    registry is reported as a structured error rather than reaching (or
    crashing) the dispatch path.
    """

    store = _make_store()
    task_id = _make_running_task(store)

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
            json.dumps({"action": "tool_call", "name": "ghost", "args": {}}),
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

    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = json.loads(tool_msgs[0].content)
    assert body["tool"] == "ghost"
    assert body["status"] == "error"
    assert body["error_code"] == "unknown_tool"


@pytest.mark.asyncio
async def test_tool_call_valid_args_reach_handler() -> None:
    """Issue 0059: schema-valid args pass validation and reach the handler.

    The companion to the invalid-args test — confirms the pre-dispatch gate is
    not over-eager: a payload that satisfies the ``args_model`` dispatches
    normally and the handler observes the validated model.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    from pydantic import BaseModel

    class _StrictArgs(BaseModel):
        value: str

    seen: list[str] = []

    async def _handler(_ctx: Any, args: BaseModel) -> SubAgentToolHandlerOutcome:
        assert isinstance(args, _StrictArgs)
        seen.append(args.value)
        return SubAgentToolHandlerOutcome(status="ok", result={"echo": args.value})

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="strict",
                version="v1",
                description="needs value",
                args_model=_StrictArgs,
                handler=_handler,
            )
        ]
    )

    client = _ScriptedClient(
        chat_values=[
            json.dumps({"action": "tool_call", "name": "strict", "args": {"value": "ok"}}),
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

    assert seen == ["ok"]
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = json.loads(tool_msgs[0].content)
    assert body["status"] == "ok"
    assert body["result"] == {"echo": "ok"}


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


# ---------------------------------------------------------------------------
# Golden envelope fixtures (PRD 0008 / issue 0057).
#
# Path 3 of the three divergent tool-calling parse paths: the sub-agent action
# envelope. ``runner._normalise_payload`` strips a code fence then ``json.loads``
# the ``{"action":…}`` envelope and validates it via ``parse_action``. These
# fixtures lock the CURRENT behaviour so the later guided-JSON phase (issue
# 0060) can flip exactly the ``parses=False`` cases to ``True`` against named
# fixtures. Fixture data lives in ``tests/fixtures/tool_calling.py``; paths 1
# and 2 are locked in ``tests/test_llm_client.py``.
#
# Key current-behaviour facts asserted below (verified against the code — the
# issue text predates the fence-strip in ``_normalise_payload``):
#   * clean + cleanly-fenced (```` ```json ```` / bare ```` ``` ````) envelopes
#     PARSE today (the fence is stripped before ``json.loads``);
#   * prose-prefix, prose-suffix, fenced-with-trailing-prose, and a non-``json``
#     fence language all FAIL today (``SubAgentActionParseError`` → the runner
#     forces ``done(failed, invalid_output)``).
# ---------------------------------------------------------------------------


_ENVELOPE_PARSES = tuple(fx for fx in ENVELOPE_FIXTURES if fx.parses)
_ENVELOPE_FAILS = tuple(fx for fx in ENVELOPE_FIXTURES if not fx.parses)


@pytest.mark.parametrize("fx", _ENVELOPE_PARSES, ids=lambda fx: fx.id)
def test_golden_envelope_parses_today(fx: EnvelopeFixture) -> None:
    """Well-formed + cleanly-fenced envelopes are accepted by the runner today."""

    normalised = _normalise_payload(fx.raw)
    assert normalised.action is not None
    assert normalised.action.action == fx.expected_action
    for key, expected in fx.expected_fields.items():
        assert getattr(normalised.action, key) == expected


@pytest.mark.parametrize("fx", _ENVELOPE_FAILS, ids=lambda fx: fx.id)
def test_golden_envelope_fails_today(fx: EnvelopeFixture) -> None:
    """Prose-wrapped / fence-with-trailing-prose / wrong-fence envelopes fail today.

    ``_strip_code_fence`` only strips a fence whose last line is the closer, so
    surrounding prose survives and ``json.loads`` raises — the runner converts
    that to ``done(failed, invalid_output)``. Locking this lets the guided-JSON
    phase flip these specific cases to "parses" and prove the win.
    """

    with pytest.raises(SubAgentActionParseError):
        _normalise_payload(fx.raw)


def test_golden_envelope_fixture_coverage() -> None:
    """Guard: both branches are exercised (no silent empty parametrization)."""

    assert _ENVELOPE_PARSES, "expected at least one parsing envelope fixture"
    assert _ENVELOPE_FAILS, "expected at least one failing envelope fixture"


# ---------------------------------------------------------------------------
# Guided-JSON envelope (PRD 0008 / issue 0060).
#
# On a backend that token-gates guided JSON (LM Studio's
# ``response_format: json_schema``) the sub-agent's control envelope is emitted
# under constrained decoding derived from the ``SubAgentAction`` union, so a
# fenced / prose-wrapped / ``json.loads``-failing envelope is impossible by
# construction. These tests assert the WIRING (the ``response_format`` payload
# handed to the client) and the constrained-reply PARSE — a live LM Studio
# round-trip is a manual smoke gate, not reproducible here. The non-guided
# (Claude CLI) envelope path stays on the tolerant ``_normalise_payload`` parse
# and is asserted unchanged (``schema`` arg never set).
# ---------------------------------------------------------------------------


def test_response_schema_is_flat_and_derived_from_union() -> None:
    """The guided ``response_format`` schema is derived from ``SubAgentAction``.

    Single source: the discriminator enum and every branch field come off the
    real action models, never a hand-written second copy. Accommodation: the
    schema must be FLAT — local / OpenAI-compatible guided decoders reject the
    top-level ``oneOf`` + ``$ref`` + ``$defs`` Pydantic emits for the union (and
    the ``anyOf`` on ``done.ui_payload``). We assert none of those constructs
    survive and that only ``action`` is required at the envelope level (so a
    ``progress`` reply need not carry ``done``'s ``status`` / ``reason_code`` —
    ``parse_action`` enforces the per-branch contract post-decode).
    """

    response_format = sub_agent_action_response_schema()
    assert response_format["name"] == SUB_AGENT_ACTION_SCHEMA_NAME

    schema = response_format["schema"]
    blob = json.dumps(schema)
    # No construct the guided decoder chokes on (this is the whole point — we do
    # NOT build the general flattener here, just keep the envelope expressible).
    for forbidden in ("oneOf", "anyOf", "$ref", "$defs"):
        assert forbidden not in blob, f"guided schema must not contain {forbidden}"

    # Discriminator enum read off the three action models (order-independent).
    assert set(schema["properties"]["action"]["enum"]) == {"progress", "tool_call", "done"}
    # Only the discriminator is required at the envelope level.
    assert schema["required"] == ["action"]
    # Permissive bag so the constrained decode keeps fields the union accepts
    # (``schema_version`` default, ``ui_payload`` which is dropped from the typed
    # grammar but still admitted on the wire — typed in issue 0065).
    assert schema["additionalProperties"] is True
    # Every flat branch field is merged in (the loose ``anyOf`` ui_payload is the
    # one intentionally-dropped field; it still rides through ``additionalProperties``).
    merged = set(schema["properties"])
    assert {
        "action",
        "thought",
        "name",
        "args",
        "result_summary",
        "status",
        "reason_code",
        "cost",
    } <= merged
    assert "ui_payload" not in merged


@pytest.mark.asyncio
async def test_guided_backend_passes_response_format_schema() -> None:
    """Acceptance: on a guided backend ``chat()`` receives the envelope schema.

    The runner asks the client ``supports_guided_json()``; when True it passes
    the ``SubAgentAction`` schema as ``chat(schema=…)`` on EVERY iteration —
    which :class:`bob.llm_client.LMStudioClient` turns into ``response_format:
    {"type": "json_schema", …}`` (constrained decode). Here we drive a
    ``progress`` then ``done`` so two calls are made and assert BOTH carry the
    schema (the constraint is per-call, not first-call-only).
    """

    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(
        chat_values=[_progress_payload("thinking"), _done_v2_payload(result_summary="ok")],
        guided=True,
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    assert len(client.calls) == 2
    expected = sub_agent_action_response_schema()
    for call in client.calls:
        assert call["schema"] == expected
        # The exact payload that becomes ``response_format.json_schema`` on
        # LM Studio carries the stable name + the flat envelope schema.
        assert call["schema"]["name"] == SUB_AGENT_ACTION_SCHEMA_NAME

    task = store.get_task(task_id)
    assert task.state == "done"


@pytest.mark.asyncio
async def test_non_guided_backend_omits_response_format_schema() -> None:
    """Acceptance: Claude CLI (non-guided) envelope path is unchanged.

    A client that does not declare ``supports_guided_json`` must receive
    ``schema=None`` so its behaviour is byte-for-byte the pre-0060 path (the CLI
    only appends a schema to the prompt as prose anyway — no token gating — so it
    stays on the tolerant ``_normalise_payload`` parse). This is the guard that
    0060 is ADDITIVE and does not leak guided decoding onto non-guided backends.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(
        chat_values=[_done_v2_payload(result_summary="ok")],
        guided=False,
    )
    assert client.supports_guided_json() is False
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    assert len(client.calls) == 1
    assert client.calls[0]["schema"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected_state"),
    [
        # ``done`` — terminal, lands the task.
        (
            json.dumps(
                {
                    "action": "done",
                    "result_summary": "all good",
                    "ui_payload": None,
                    "status": "complete",
                    "reason_code": "ok",
                    "cost": {},
                }
            ),
            "done",
        ),
    ],
)
async def test_guided_clean_reply_parses_done(reply: str, expected_state: str) -> None:
    """A constrained (clean-JSON) ``done`` reply parses + terminates the run.

    Under guided decoding the reply is always a clean ``{"action": …}`` object,
    so ``_normalise_payload`` → ``parse_action`` succeeds trivially. This is the
    happy path the live LM Studio smoke exercises.
    """

    store = _make_store()
    task_id = _make_running_task(store)

    client = _ScriptedClient(chat_values=[reply], guided=True)
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    assert store.get_task(task_id).state == expected_state


@pytest.mark.asyncio
async def test_guided_progress_then_tool_call_then_done_all_parse() -> None:
    """``progress`` / ``tool_call`` / ``done`` all parse on the guided path.

    Exercises every envelope branch as a clean constrained reply through a full
    run (with a stub tool so the ``tool_call`` dispatches), proving the guided
    wiring carries the schema AND the three actions round-trip without touching
    the fence/prose tolerance.
    """

    store = _make_store()
    task_id = _make_running_task(store)

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
            json.dumps({"action": "progress", "thought": "step 1"}),
            json.dumps({"action": "tool_call", "name": "noop", "args": {"value": "hi"}}),
            _done_v2_payload(result_summary="done"),
        ],
        guided=True,
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
    # All three calls carried the guided schema (per-call constraint).
    assert len(client.calls) == 3
    assert all(call["schema"] is not None for call in client.calls)
    # The tool_call dispatched (its result round-tripped into the task log).
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "noop" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_guided_path_avoids_fenced_envelope_failure_mode() -> None:
    """The production failure (fenced ``progress`` → ``llm_failed``) is unreachable.

    Regression for ``backend/logs/orchestration.jsonl`` (2026-05-28): a local
    model emitted a markdown-fenced envelope, ``json.loads`` choked and the task
    died ``llm_failed``. We reproduce the SHAPE of that win: the non-guided path
    still FAILS on a fenced-with-trailing-prose envelope (the tolerant strip
    can't save it), but the guided path never sees that shape — under constrained
    decode the reply is clean JSON. We assert the contrast directly: the same
    runner construction yields a parse failure off the raw fenced string via the
    non-guided normaliser, while the guided run with a clean reply succeeds.
    """

    # The fenced-with-trailing-prose shape that defeats the non-guided strip
    # (locked by the 0057 path-3 ``envelope/fenced-trailing-prose`` fixture).
    fenced_with_prose = (
        "```json\n" + json.dumps({"action": "progress", "thought": "x"}) + "\n```\nDone thinking."
    )
    # Non-guided normaliser still raises on it (unchanged fallback behaviour).
    with pytest.raises(SubAgentActionParseError):
        _normalise_payload(fenced_with_prose)

    # Guided backend: the model is constrained, so the reply is clean JSON and
    # the run completes — the fenced failure mode is simply not produced.
    store = _make_store()
    task_id = _make_running_task(store)
    client = _ScriptedClient(
        chat_values=[
            json.dumps({"action": "progress", "thought": "x"}),
            _done_v2_payload(result_summary="ok"),
        ],
        guided=True,
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    # Completed cleanly — NOT the ``failed`` / ``llm_failed`` the bug produced.
    assert task.state == "done"
