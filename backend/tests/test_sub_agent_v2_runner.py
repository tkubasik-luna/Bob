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
from bob.debug_log import snapshot_for_task
from bob.event_bus import EventBus
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent import (
    REASON_HARD_KILLED,
    REASON_ITERATION_CAP,
    REASON_OK,
    REASON_STALLED,
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
from bob.sub_agent.runner import (
    _model_payload_to_sections,
    _normalise_payload,
    _resolve_terminal_deliverable,
)
from bob.sub_agent.tool_registry import (
    WebSearchArgs,
    build_gmail_search_tool,
    build_web_fetch_tool,
    build_web_search_tool,
    project_gmail_search,
)
from bob.task_store import TaskStore
from bob.ui_registry import ComponentDescriptor

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
    ui_payload: dict[str, Any] | str | None = None,
) -> str:
    return json.dumps(
        {
            "action": "done",
            "result_summary": result_summary,
            "ui_payload": ui_payload,
            "status": status,
            "reason_code": reason_code,
            "cost": cost or {},
        }
    )


def _valid_mail_props() -> dict[str, Any]:
    """A Mail descriptor's props that satisfy the single ``ui_registry`` schema —
    the shape a ``done`` deliverable carries for a mail-overlay task (issue 0065)."""

    return {
        "from": {"name": "Marie Lefèvre", "email": "marie@lunabee.com"},
        "receivedAt": "2026-05-28T14:22:00Z",
        "subject": "Q3 forecast",
        "bodyPreview": "deck ready by Thursday?",
        "threadId": "thread-001",
        "messageId": "msg-001",
        "gmailWebUrl": "https://mail.google.com/mail/u/0/#inbox/thread-001",
    }


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------


def test_schema_version_is_three() -> None:
    """Acceptance: ``schema_version`` constant is 3 (bumped in PRD 0009 when
    ``done`` gained the optional ``result_ref`` store handle; was 2 in issue
    0065 for the typed ``Deliverable`` union)."""

    assert SUB_AGENT_SCHEMA_VERSION == 3


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
    # Issue 0065: a structured ui_payload is coerced to the typed
    # ComponentDescriptor branch of the Deliverable union (parse_action gates
    # the SHAPE; the runner gates the props against ui_registry).
    assert isinstance(action.ui_payload, ComponentDescriptor)
    assert action.ui_payload.component == "Markdown"
    assert action.schema_version == 3


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
    """Issue 0059 + 0063: ``to_spec()`` derives + flattens the args schema.

    The spec's ``parameters`` is the ``args_model`` JSON Schema run through
    :func:`flatten_schema` (0063) — equal to the flattened schema, NOT the raw
    Pydantic output — and the spec retains the model so the self-correction
    phase can re-validate against it. Name/description pass through unchanged.
    """

    from bob.llm.tooling import flatten_schema

    definition = build_gmail_search_tool()
    spec = definition.to_spec()

    assert spec.name == "gmail_search"
    assert spec.description == definition.description
    assert spec.args_model is GmailSearchArgs
    # Parameters derived from the Pydantic model and flattened (0063 contract).
    assert spec.parameters == flatten_schema(GmailSearchArgs.model_json_schema())
    assert spec.parameters["type"] == "object"
    # Every structured filter field is present in the derived schema.
    assert set(spec.parameters["properties"]) == set(GmailSearchArgs.model_fields)
    # 0063: the ``Optional[...]`` fields no longer carry an ``anyOf`` — the
    # flattener collapsed ``[{type}, {null}]`` to the single scalar branch.
    assert all("anyOf" not in prop for prop in spec.parameters["properties"].values())


def test_sub_agent_v2_prompt_documents_inbox_fallback() -> None:
    """Regression for the 2026-05-28 12:24 ``iteration_cap`` post-mortem.

    Task ``73146d06…`` ("Dernier mail reçu") looped 24 times because the LLM
    kept calling ``gmail_search`` with no filter and the validator rejected
    every attempt with ``error_code: invalid_args``. The recipe MUST instruct
    the model to fall back to ``label="INBOX"`` (received) or ``label="SENT"``
    (sent) when the goal is generic, so a single rejected call is impossible.

    Issue 0063 moved the recipe out of the base prompt into the Gmail skill
    pack — the fallback guidance lives there now (and is loaded for any
    mail-shaped goal), so the regression assertion follows it to the pack.
    """

    from bob.context.prompt_fragments import GMAIL_SEARCH_SKILL_PACK

    rendered = GMAIL_SEARCH_SKILL_PACK.render()
    assert 'label="INBOX"' in rendered
    assert 'label="SENT"' in rendered
    # Must be framed as a fallback for the generic-goal branch, not as an
    # always-on default (the specific-filter happy path stays untouched).
    assert "Fallback" in rendered or "fallback" in rendered
    # And the base contract no longer carries the recipe — it stays tool-agnostic.
    from bob.context.prompt_fragments import SUB_AGENT_V2_SYSTEM_PROMPT

    assert "gmail_search" not in SUB_AGENT_V2_SYSTEM_PROMPT.render(goal="dummy")


def _make_prompt_runner(store: TaskStore) -> SubAgentRunner:
    """A runner with an EMPTY tool registry — so the only source of recipe text
    in the built system prompt is a loaded skill pack, never the tool catalogue.
    ``chat()`` is never invoked, so the scripted client needs no canned values."""

    runner = SubAgentRunner(
        subagent_client=_ScriptedClient(),
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=1, wall_clock_seconds=999.0, token_cap=10_000),
        clock=_ControllableClock(),
    )
    # Force an empty catalogue post-construction: an empty registry passed to the
    # ctor is coerced away by ``tool_registry or build_default_subagent_registry()``
    # (an empty registry is falsy via ``__len__``), which would re-introduce the
    # gmail_search tool name and defeat the isolation this helper exists for.
    runner._tool_registry = SubAgentToolRegistry()
    return runner


def test_build_messages_loads_gmail_skill_pack_for_mail_goal() -> None:
    """Issue 0063: a mail-shaped goal pulls the Gmail skill pack into the system
    prompt, so the recipe's INBOX/SENT fallback guidance is present at runtime."""

    store = _make_store()
    task_id = _make_running_task(store, goal="Trouve le dernier mail reçu de Paul")
    runner = _make_prompt_runner(store)

    messages = runner._build_messages(store.get_task(task_id), [])

    assert messages[0]["role"] == "system"
    system = messages[0]["content"]
    assert "gmail_search" in system
    assert 'label="INBOX"' in system


def test_build_messages_omits_skill_pack_for_unrelated_goal() -> None:
    """A goal with no mail trigger never pays for the Gmail recipe's tokens — the
    base contract stays tool-agnostic (empty registry → no catalogue leak)."""

    store = _make_store()
    task_id = _make_running_task(store, goal="do the thing")  # no mail trigger
    runner = _make_prompt_runner(store)

    system = runner._build_messages(store.get_task(task_id), [])[0]["content"]

    assert "gmail_search" not in system
    assert 'label="INBOX"' not in system


def test_select_skill_packs_matches_mail_goal_only() -> None:
    """``select_skill_packs`` is the conditional-loading decision point: it
    returns the Gmail pack for any mail-shaped goal and nothing otherwise."""

    from bob.context.prompt_fragments import (
        GMAIL_SEARCH_SKILL_PACK,
        select_skill_packs,
    )

    assert select_skill_packs("Lis ma boîte mail") == [GMAIL_SEARCH_SKILL_PACK]
    assert select_skill_packs("check my inbox please") == [GMAIL_SEARCH_SKILL_PACK]
    assert select_skill_packs("refactor the JSON parser") == []


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
async def test_tool_call_invalid_args_routes_under_system_validator_not_tool() -> None:
    """Issue 0062: invalid args feed back under ``system_validator``, not ``tool``.

    The handler MUST NOT run with a malformed payload (no blind dispatch). The
    pre-0062 path round-tripped the structured error as a ``tool`` message —
    echoing the model's own bad output back under a role it is trained to
    trust. 0062 routes the correction under the ``system_validator`` role
    instead (PRD 0006 prompt-injection safety), bounded by the per-tool
    RetryPolicy. Here ``value`` is required but omitted; the model retries and
    the scripted ``done`` ends the task.
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
            _done_v2_payload(result_summary="ok now", status="complete", reason_code="ok"),
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

    # No ``tool`` message carries the validation error — it never round-trips
    # under the trusted ``tool`` role (the core 0062 security change).
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs == []

    # The correction was injected under ``system_validator`` on the retry call.
    assert len(client.calls) == 2
    retry_messages = client.calls[1]["messages"]
    validator_rows = [m for m in retry_messages if m["role"] == "system_validator"]
    assert len(validator_rows) == 1
    # Names the offending field + carries the escaped offending output.
    assert "value" in validator_rows[0]["content"]
    assert "invalid_args" in validator_rows[0]["content"]
    assert "[INVALID OUTPUT]:" in validator_rows[0]["content"]


@pytest.mark.asyncio
async def test_tool_call_unknown_tool_routes_under_system_validator() -> None:
    """Issue 0062: an unknown tool name feeds back under ``system_validator``.

    Validation resolves the tool by name BEFORE dispatch; a name not in the
    registry is an ``unknown_tool`` mistake by the model. Like invalid args
    (0062), the correction rides the ``system_validator`` role — never the
    ``tool`` role — bounded by the RetryPolicy.
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

    # Never round-trips under the ``tool`` role.
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert tool_msgs == []

    # Correction injected under ``system_validator`` on the retry call.
    assert len(client.calls) == 2
    validator_rows = [m for m in client.calls[1]["messages"] if m["role"] == "system_validator"]
    assert len(validator_rows) == 1
    assert "unknown_tool" in validator_rows[0]["content"]
    assert "ghost" in validator_rows[0]["content"]


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
# Self-correction loop for tool-arg validation (issue 0062)
# ---------------------------------------------------------------------------


def _strict_value_registry(handler_calls: list[Any]) -> SubAgentToolRegistry:
    """A one-tool registry whose ``strict`` tool requires a string ``value``."""

    from pydantic import BaseModel

    class _StrictArgs(BaseModel):
        value: str

    async def _handler(_ctx: Any, args: BaseModel) -> SubAgentToolHandlerOutcome:
        handler_calls.append(args)
        return SubAgentToolHandlerOutcome(status="ok", result={"echo": args.model_dump()["value"]})

    return SubAgentToolRegistry(
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


@pytest.mark.asyncio
async def test_invalid_tool_args_recover_on_retry() -> None:
    """Issue 0062: a bad call corrected on the retry dispatches normally.

    The model emits ``strict`` with no ``value`` (rejected), gets the
    ``system_validator`` correction, then emits a valid call which dispatches
    to the handler. The retry budget then RESETS — proving a recovered call is
    not penalised for the earlier mistake.
    """

    store = _make_store()
    task_id = _make_running_task(store)
    handler_calls: list[Any] = []
    registry = _strict_value_registry(handler_calls)

    client = _ScriptedClient(
        chat_values=[
            json.dumps({"action": "tool_call", "name": "strict", "args": {}}),  # invalid
            json.dumps(
                {"action": "tool_call", "name": "strict", "args": {"value": "ok"}}
            ),  # corrected
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

    # The handler ran exactly once — with the corrected args.
    assert len(handler_calls) == 1
    assert handler_calls[0].model_dump()["value"] == "ok"

    # Exactly one tool message — the SUCCESSFUL dispatch, not the rejection.
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0].content)["status"] == "ok"

    # Validator feedback rode the retry (call index 1) but was dropped after
    # the successful dispatch, so the done call (index 2) carries none.
    assert any(m["role"] == "system_validator" for m in client.calls[1]["messages"])
    assert not any(m["role"] == "system_validator" for m in client.calls[2]["messages"])

    assert store.get_task(task_id).state == "done"


@pytest.mark.asyncio
async def test_invalid_tool_args_exhaust_to_forced_done() -> None:
    """Issue 0062: repeated invalid args exhaust the budget → forced done(failed).

    With the default per-tool policy (``max_retries=1``) a second consecutive
    invalid call spends the budget. The exhaustion path is EXPLICIT — a forced
    ``done(failed, invalid_output)`` — never a silent drop and never an
    unbounded round-trip.
    """

    store = _make_store()
    task_id = _make_running_task(store)
    handler_calls: list[Any] = []
    registry = _strict_value_registry(handler_calls)

    client = _ScriptedClient(
        chat_values=[
            json.dumps({"action": "tool_call", "name": "strict", "args": {}}),
            json.dumps({"action": "tool_call", "name": "strict", "args": {}}),
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

    # Budget = 1 retry → exactly 2 LLM calls, then forced failure.
    assert len(client.calls) == 2
    assert handler_calls == []

    task = store.get_task(task_id)
    assert task.state == "failed"
    # Forced done(failed, invalid_output) records the reason as a ``system``
    # message (failed rows keep ``task.result`` None by design).
    system_msgs = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    assert any("invalid" in m.content.lower() for m in system_msgs)


@pytest.mark.asyncio
async def test_validation_feedback_never_uses_tool_role() -> None:
    """Issue 0062 security: arg-validation feedback NEVER uses the ``tool`` role.

    PRD 0006 forbids echoing the model's own malformed output back under a
    role it is trained to trust. This locks the invariant across the whole run:
    the only validation feedback role is ``system_validator``, and not a single
    ``tool`` message is persisted when every call is rejected pre-dispatch. The
    escaped ``[INVALID OUTPUT]:`` marker confirms the offending payload is
    neutralised before re-injection.
    """

    store = _make_store()
    task_id = _make_running_task(store)
    handler_calls: list[Any] = []
    registry = _strict_value_registry(handler_calls)

    client = _ScriptedClient(
        chat_values=[
            json.dumps({"action": "tool_call", "name": "strict", "args": {}}),
            json.dumps({"action": "tool_call", "name": "strict", "args": {}}),
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

    # No persisted ``tool`` message — validation errors never round-trip there.
    assert [m for m in store.get_task_messages(task_id) if m.role == "tool"] == []

    # Across every LLM call, validation feedback used ``system_validator`` only.
    validator_rows = [
        m for call in client.calls for m in call["messages"] if m["role"] == "system_validator"
    ]
    assert validator_rows  # at least one correction was injected
    assert all("[INVALID OUTPUT]:" in row["content"] for row in validator_rows)
    # Nothing in any injected message claims the ``tool`` role as a carrier for
    # the rejection text.
    for call in client.calls:
        for m in call["messages"]:
            if m["role"] == "tool":
                assert "invalid_args" not in m["content"]


# ---------------------------------------------------------------------------
# Deliverable union — typed + validated end-to-end (issue 0065)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("guided", [False, True])
@pytest.mark.asyncio
async def test_done_markdown_string_deliverable_finalises(guided: bool) -> None:
    """Issue 0065 + 0066: a markdown-string deliverable finalises on BOTH
    backends (guided LM-Studio-style + non-guided Claude-CLI-style).

    A bare string is the ``MarkdownDeliverable`` branch of the ``Deliverable``
    union — the shape the model naturally emits for a document-class task. It
    carries no structured contract, so it is always valid. PRD 0010 / issue
    0066 — it now travels as a list-of-one ``Markdown`` section in
    ``task.result_payload`` (so the SectionsOverlay renders it through the
    registry), while the markdown text still lands in ``task.result`` for the
    text fallback / recall path.
    """

    store = _make_store()
    task_id = _make_running_task(store)
    client = _ScriptedClient(
        chat_values=[
            _done_v2_payload(
                result_summary="exposé prêt",
                ui_payload="# Exposé\n\nLe corps du document.",
            )
        ],
        guided=guided,
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
    assert task.state == "done"
    assert task.result == "# Exposé\n\nLe corps du document."
    # PRD 0010 / issue 0066 — a document-class deliverable travels as a
    # list-of-one Markdown section so the SectionsOverlay renders it.
    assert task.result_payload == [
        {"component": "Markdown", "props": {"content": "# Exposé\n\nLe corps du document."}}
    ]
    # No correction loop — a string deliverable is valid by construction.
    assert len(client.calls) == 1
    assert not any(
        m["role"] == "system_validator" for call in client.calls for m in call["messages"]
    )


@pytest.mark.parametrize("guided", [False, True])
@pytest.mark.asyncio
async def test_done_component_descriptor_deliverable_finalises(guided: bool) -> None:
    """Issue 0065: a valid ``{component, props}`` deliverable survives validation
    and is carried STRUCTURED to the frontend on BOTH backends.

    A descriptor whose props satisfy the single ``ui_registry`` schema is the
    ``ComponentDescriptor`` branch of the union. The runner validates it, then
    normalises it back to a plain dict so 0064's structured transport
    (``task.result_payload`` + ``task_result`` WS event) carries it unflattened.
    """

    store = _make_store()
    task_id = _make_running_task(store)
    client = _ScriptedClient(
        chat_values=[
            _done_v2_payload(
                result_summary="Mail de Marie, sujet 'Q3 forecast'.",
                ui_payload={"component": "Mail", "props": _valid_mail_props()},
            )
        ],
        guided=guided,
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
    assert task.state == "done"
    # Structured descriptor carried via result_payload, NOT flattened to text.
    assert task.result_payload == [{"component": "Mail", "props": _valid_mail_props()}]
    # The spoken summary stays the text result (Mail props live in the payload).
    assert task.result == "Mail de Marie, sujet 'Q3 forecast'."
    # Valid descriptor → no correction loop.
    assert len(client.calls) == 1
    assert not any(
        m["role"] == "system_validator" for call in client.calls for m in call["messages"]
    )


@pytest.mark.parametrize("guided", [False, True])
@pytest.mark.asyncio
async def test_done_invalid_descriptor_routes_under_system_validator(guided: bool) -> None:
    """Issue 0065 acceptance: an invalid deliverable triggers the P5
    self-correction loop, NOT a silent drop — on both backends.

    The first ``done`` carries a Mail descriptor missing the required ``from``
    field. The runner validates it against the single ``ui_registry`` schema,
    rejects it, and feeds the correction back under the ``system_validator``
    role (NEVER ``tool`` — PRD 0006 prompt-injection safety). The model then
    emits a valid descriptor which finalises normally.
    """

    store = _make_store()
    task_id = _make_running_task(store)
    bad_props = _valid_mail_props()
    del bad_props["from"]  # required → schema violation
    client = _ScriptedClient(
        chat_values=[
            _done_v2_payload(
                result_summary="here",
                ui_payload={"component": "Mail", "props": bad_props},
            ),
            _done_v2_payload(
                result_summary="Mail de Marie.",
                ui_payload={"component": "Mail", "props": _valid_mail_props()},
            ),
        ],
        guided=guided,
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    # Rejected once, corrected on the retry → exactly two LLM calls.
    assert len(client.calls) == 2

    # The correction rode the ``system_validator`` role on the retry call, and
    # named the offending deliverable field + the escaped offending output.
    validator_rows = [m for m in client.calls[1]["messages"] if m["role"] == "system_validator"]
    assert len(validator_rows) == 1
    assert "ui_payload" in validator_rows[0]["content"]
    assert "[INVALID OUTPUT]:" in validator_rows[0]["content"]

    # The invalid deliverable NEVER round-trips under the trusted ``tool`` role.
    assert [m for m in store.get_task_messages(task_id) if m.role == "tool"] == []

    # The corrected descriptor finalised the task with the structured payload.
    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result_payload == [{"component": "Mail", "props": _valid_mail_props()}]


@pytest.mark.asyncio
async def test_done_invalid_descriptor_exhausts_to_forced_done() -> None:
    """Issue 0065: a deliverable that stays invalid spends the retry budget and
    forces an EXPLICIT ``done(failed, invalid_output)`` — never a silent drop.

    Mirrors the ``tool_call.args`` exhaustion path (0062): the deliverable
    validation rides the SAME envelope retry budget, so a second consecutive
    invalid descriptor exhausts it and the shared handler forces a terminal
    failure recorded as a ``system`` message.
    """

    store = _make_store()
    task_id = _make_running_task(store)
    bad_props = _valid_mail_props()
    del bad_props["from"]
    client = _ScriptedClient(
        chat_values=[
            _done_v2_payload(ui_payload={"component": "Mail", "props": bad_props}),
            _done_v2_payload(ui_payload={"component": "Mail", "props": bad_props}),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    # Budget = 1 retry → exactly two calls, then forced failure.
    assert len(client.calls) == 2
    task = store.get_task(task_id)
    assert task.state == "failed"
    # The reason is recorded as a ``system`` message (failed rows keep no result).
    system_msgs = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    assert any("invalid" in m.content.lower() for m in system_msgs)
    # The invalid deliverable never round-tripped under the ``tool`` role.
    assert [m for m in store.get_task_messages(task_id) if m.role == "tool"] == []


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


# ---------------------------------------------------------------------------
# Loop-convergence guards (mail-tool-loop investigation, 2026-05-29)
#
# A successful tool result already in context must not be discarded by a loop
# that never emits ``done``. These cover: dedup of identical tool calls (RC4),
# the stall forcing function on progress-spam (RC1), salvage of the retrieved
# data on a cap exit (RC2), and the control-char arg guard (RC5) — plus a
# happy-path guard so the forcing function does not fire too eagerly.
# ---------------------------------------------------------------------------


def _tool_call_payload(name: str, args: dict[str, Any]) -> str:
    return json.dumps({"action": "tool_call", "name": name, "args": args})


def _make_echo_registry() -> tuple[SubAgentToolRegistry, list[Any]]:
    """An ``echo`` tool returning its query as a successful result.

    Returns the registry plus a list that records each handler invocation so a
    test can assert how many times the tool actually ran — the dedup guard must
    keep that at 1 even when the model re-requests the same call.
    """

    from pydantic import BaseModel

    class _EchoArgs(BaseModel):
        q: str

    handler_calls: list[Any] = []

    async def _handler(_ctx: Any, args: BaseModel) -> SubAgentToolHandlerOutcome:
        handler_calls.append(args)
        return SubAgentToolHandlerOutcome(
            status="ok", result={"echo": getattr(args, "q", None), "found": True}
        )

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="echo",
                version="v1",
                description="echo back the query",
                args_model=_EchoArgs,
                handler=_handler,
            )
        ]
    )
    return registry, handler_calls


@pytest.mark.asyncio
async def test_duplicate_tool_call_not_redispatched_and_forces_salvaged_done() -> None:
    """RC4 + RC1 + RC2: an identical ``(name, args)`` call is suppressed (not
    re-dispatched); repeated dups drive the stall guard to a forced
    ``done(degraded, stalled_no_progress)`` carrying the salvaged result."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, handler_calls = _make_echo_registry()
    # 5 identical calls: #1 dispatches, #2..#5 are dups → stall 1,2,3,4 → force.
    client = _ScriptedClient(chat_values=[_tool_call_payload("echo", {"q": "x"}) for _ in range(5)])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=10_000_000),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    # Dispatched exactly once despite 5 identical requests.
    assert len(handler_calls) == 1
    # The suppressed dups never reached the transcript — only one tool message.
    tool_msgs = [m for m in store.get_task_messages(task_id) if m.role == "tool"]
    assert len(tool_msgs) == 1

    task = store.get_task(task_id)
    assert task.state == "done"
    assert state_changes[-1]["status"] == "degraded"
    assert state_changes[-1]["reason_code"] == REASON_STALLED
    # RC2 — the salvaged result carries the retrieved data, not an empty string.
    assert task.result is not None
    assert "résultat partiel" in task.result
    assert "x" in task.result


@pytest.mark.asyncio
async def test_progress_spam_after_tool_result_forces_salvaged_done() -> None:
    """RC1: repeated ``progress`` after a successful tool result is the stall;
    the runner force-terminates with a salvaged ``done`` + injects the nudge."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, handler_calls = _make_echo_registry()
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("echo", {"q": "olivier"}),
            _progress_payload("p1"),
            _progress_payload("p2"),
            _progress_payload("p3"),
            _progress_payload("p4"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=10_000_000),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    assert len(handler_calls) == 1
    task = store.get_task(task_id)
    assert task.state == "done"
    assert state_changes[-1]["reason_code"] == REASON_STALLED
    assert task.result is not None
    assert "olivier" in task.result
    # The forcing nudge was injected once the stall hit the nudge threshold.
    nudge_calls = [
        c for c in client.calls if any(m["role"] == "system_validator" for m in c["messages"])
    ]
    assert nudge_calls


@pytest.mark.asyncio
async def test_iteration_cap_salvages_prior_tool_result() -> None:
    """RC2: a cap firing after a successful tool call no longer discards the
    retrieved data — the degraded ``done`` carries the salvaged result instead
    of the empty string the bug produced ("aucun résultat")."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, _handler_calls = _make_echo_registry()
    # tool (iter1) + progress (iter2) + progress (iter3) → top of iter4 trips cap=3.
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("echo", {"q": "mail-data"}),
            _progress_payload("p1"),
            _progress_payload("p2"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(max_iterations=3, wall_clock_seconds=999.0, token_cap=10_000_000),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert state_changes[-1]["reason_code"] == REASON_ITERATION_CAP
    # Old behaviour: result == "". Now the data survives.
    assert task.result is not None
    assert task.result != ""
    assert "mail-data" in task.result


@pytest.mark.asyncio
async def test_control_char_in_tool_arg_routes_to_validator_not_dispatched() -> None:
    """RC5: a control char in a string arg (guided-decode UTF-8 mangling, e.g.
    ``é`` → U+0013) is rejected pre-dispatch and corrected under
    ``system_validator`` — the tool never runs on the corrupted query."""

    store = _make_store()
    task_id = _make_running_task(store)
    registry, handler_calls = _make_echo_registry()
    client = _ScriptedClient(
        chat_values=[
            # "intéressement" with é mangled to U+0013 — exactly the log artefact.
            _tool_call_payload("echo", {"q": "intressement"}),
            _done_v2_payload(result_summary="recovered", status="complete", reason_code="ok"),
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

    # The corrupted query never dispatched.
    assert handler_calls == []
    # No ``tool`` message — the rejection rode ``system_validator``, not ``tool``.
    assert [m for m in store.get_task_messages(task_id) if m.role == "tool"] == []
    # Correction injected under system_validator on the retry call.
    assert len(client.calls) == 2
    validator_rows = [m for m in client.calls[1]["messages"] if m["role"] == "system_validator"]
    assert len(validator_rows) == 1
    assert "invalid_args" in validator_rows[0]["content"]
    task = store.get_task(task_id)
    assert task.state == "done"


@pytest.mark.asyncio
async def test_single_progress_after_tool_result_still_reaches_model_done() -> None:
    """Guard against over-eager forcing: the Gmail recipe's legitimate single
    'lecture du mail' ``progress`` between the tool result and ``done`` must NOT
    trip the stall guard (nudge threshold is 2, so one is fine)."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, handler_calls = _make_echo_registry()
    client = _ScriptedClient(
        chat_values=[
            _progress_payload("recherche"),
            _tool_call_payload("echo", {"q": "x"}),
            _progress_payload("lecture du résultat"),
            _done_v2_payload(result_summary="done cleanly", status="complete", reason_code="ok"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    assert len(handler_calls) == 1
    task = store.get_task(task_id)
    assert task.state == "done"
    # Reached the model's own ``done`` (complete/ok), NOT a forced stall/cap.
    assert state_changes[-1]["status"] == "complete"
    assert state_changes[-1]["reason_code"] == "ok"
    assert task.result == "done cleanly"


@pytest.mark.asyncio
async def test_salvaged_result_redacted_from_debug_sink_but_reaches_chat() -> None:
    """RC2 privacy (issue 0056): a salvaged tool result reaches the chat client
    via ``task.result`` but its raw body is scrubbed from the debug ring buffer /
    ``/ws/debug`` feed / JSONL sink — the loop fix must not regress mail privacy
    by dumping an email ``bodyPreview`` into the debug envelopes."""

    from pydantic import BaseModel

    secret = "TOPSECRET-BODY-XYZ"

    class _MailishArgs(BaseModel):
        q: str

    async def _handler(_ctx: Any, _args: BaseModel) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(
            status="ok", result={"bodyPreview": secret, "found": True}
        )

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="echo",
                version="v1",
                description="returns a sensitive body",
                args_model=_MailishArgs,
                handler=_handler,
            )
        ]
    )
    store = _make_store()
    task_id = _make_running_task(store)
    # 5 identical calls → dispatch once, then stall-force a salvaged done.
    client = _ScriptedClient(chat_values=[_tool_call_payload("echo", {"q": "x"}) for _ in range(5)])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=10_000_000),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    # Chat side: the salvaged result carries the data so Jarvis can still answer.
    task = store.get_task(task_id)
    assert task.result is not None
    assert secret in task.result

    # Debug side: NO captured debug event may echo the raw salvaged body.
    captured = [event.to_dict() for event in snapshot_for_task(task_id)]
    assert captured  # sanity — events were captured for the task
    for ev in captured:
        assert secret not in json.dumps(ev, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Loop-convergence guards — Trou A/B (mail-tool-loop follow-up, 2026-05-29)
#
# The original RC1 guard only counted a stall AFTER a successful tool result
# (``last_tool_result is not None``). Two gaps that the "Dernier mail du jour"
# hang exposed (gmail_search ``after:"today"`` → invalid_query → 23 ``progress``
# → external hard-kill with an empty "Raison brute"):
#   * Trou A — consecutive ``progress`` with no usable result is unbounded;
#   * Trou B — a tool that ERRORS leaves ``last_tool_result`` None, so the guard
#     never armed even though the run was clearly looping.
# These cover both: progress always counts; an errored dispatch counts; recovery
# resets; and a failed(stalled) exit surfaces the tool error to the chat client.
# ---------------------------------------------------------------------------


def _make_erroring_registry(
    *, error_message: str = "boom", error_code: str = "tool_boom"
) -> SubAgentToolRegistry:
    """A one-tool registry whose ``boomtool`` ALWAYS returns ``status=error`` —
    the runtime tool-error shape (gmail_search invalid_query) that Trou B is about."""

    from pydantic import BaseModel

    class _Args(BaseModel):
        q: str

    async def _handler(_ctx: Any, _args: BaseModel) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(
            status="error", error_code=error_code, error_message=error_message
        )

    return SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="boomtool",
                version="v1",
                description="always errors",
                args_model=_Args,
                handler=_handler,
            )
        ]
    )


def _make_flaky_registry() -> tuple[SubAgentToolRegistry, list[Any]]:
    """A ``flaky`` tool that ERRORS on ``q == "bad"`` and succeeds otherwise —
    lets a test prove a corrected retry RESETS the stall streak (no over-fire)."""

    from pydantic import BaseModel

    class _Args(BaseModel):
        q: str

    handler_calls: list[Any] = []

    async def _handler(_ctx: Any, args: BaseModel) -> SubAgentToolHandlerOutcome:
        handler_calls.append(args)
        if getattr(args, "q", None) == "bad":
            return SubAgentToolHandlerOutcome(
                status="error", error_code="tool_boom", error_message="bad query"
            )
        return SubAgentToolHandlerOutcome(
            status="ok", result={"echo": getattr(args, "q", None), "found": True}
        )

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="flaky",
                version="v1",
                description="errors on q=='bad'",
                args_model=_Args,
                handler=_handler,
            )
        ]
    )
    return registry, handler_calls


@pytest.mark.asyncio
async def test_progress_spam_without_any_tool_forces_failed_stalled() -> None:
    """Trou A: consecutive ``progress`` with NO tool call ever is now bounded.

    The original RC1 guard only counted progress AFTER a successful tool result,
    so a pure-progress spin (a weak model "thinking" forever) ran free until a
    hard cap / external kill. Now EVERY progress counts: nudge at 2, force at 4 —
    far below the (deliberately high) iteration cap, proving the stall guard, not
    the cap, is what terminates it."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    # Many progress, no tool, no done. High caps so ONLY the stall guard can fire.
    client = _ScriptedClient(chat_values=[_progress_payload(f"thinking {i}") for i in range(20)])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=10_000_000),
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    # Forced at the 4th consecutive progress (stall 1,2,3,4) — not the 99 cap.
    assert len(client.calls) == 4
    task = store.get_task(task_id)
    assert task.state == "failed"
    assert state_changes[-1]["status"] == "failed"
    assert state_changes[-1]["reason_code"] == REASON_STALLED
    # The "no tool" nudge was injected once the stall hit the nudge threshold.
    nudges = [
        m["content"] for c in client.calls for m in c["messages"] if m["role"] == "system_validator"
    ]
    assert nudges
    assert any("sans appeler d'outil" in n for n in nudges)


@pytest.mark.asyncio
async def test_tool_error_then_progress_spam_forces_failed_naming_the_error() -> None:
    """Trou B: a tool that ERRORS leaves ``last_tool_result`` None — the original
    RC1 guard never armed, so the model looped on ``progress`` until a hard-kill
    (the "Dernier mail du jour" production hang). Now an errored dispatch counts
    toward the stall guard; the run force-terminates ``failed(stalled)`` and the
    reason — naming the tool error — reaches ``task.result`` so the orchestrator's
    failed-synthesis can explain it instead of an empty "Raison brute"."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry = _make_erroring_registry(
        error_message="after must be a YYYY-MM-DD string or datetime.date: got 'today'"
    )
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("boomtool", {"q": "x"}),  # errors → stall 1
            _progress_payload("j'ai appelé l'outil"),  # stall 2 → nudge
            _progress_payload("je relance la recherche"),  # stall 3
            _progress_payload("toujours en cours"),  # stall 4 → force
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=10_000_000),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    # Forced at stall 4 (1 error + 3 progress), not the 99-iteration cap.
    assert len(client.calls) == 4
    task = store.get_task(task_id)
    assert task.state == "failed"
    assert state_changes[-1]["status"] == "failed"
    assert state_changes[-1]["reason_code"] == REASON_STALLED
    # Trou B — the reason reaches task.result (was an empty "Raison brute" before).
    assert task.result is not None
    assert "after must be a YYYY-MM-DD" in task.result
    assert "boomtool" in task.result
    # The error-AWARE nudge (naming the failure) was injected, not the generic one.
    nudges = [
        m["content"] for c in client.calls for m in c["messages"] if m["role"] == "system_validator"
    ]
    assert any("a échoué" in n and "YYYY-MM-DD" in n for n in nudges)


@pytest.mark.asyncio
async def test_tool_error_then_successful_retry_resets_stall() -> None:
    """Trou B must not over-fire: a tool error FOLLOWED by a corrected successful
    call is genuine recovery. The fresh successful result resets the stall streak,
    so the model reaches its OWN ``done`` (complete/ok) — never a forced stall."""

    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, handler_calls = _make_flaky_registry()
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("flaky", {"q": "bad"}),  # error → stall 1
            _tool_call_payload("flaky", {"q": "good"}),  # ok → stall reset to 0
            _done_v2_payload(result_summary="done cleanly", status="complete", reason_code="ok"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    # Both calls ran (the error, then the recovery); the run ended on the model's
    # own done, not a forced stall.
    assert len(handler_calls) == 2
    task = store.get_task(task_id)
    assert task.state == "done"
    assert state_changes[-1]["status"] == "complete"
    assert state_changes[-1]["reason_code"] == "ok"
    assert task.result == "done cleanly"
    # The single error (stall 1, below the nudge threshold of 2) injected no nudge.
    assert not any(m["role"] == "system_validator" for c in client.calls for m in c["messages"])


# ---------------------------------------------------------------------------
# PRD 0009 P3 — tool result store + compact transcript digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p3_tool_result_transcript_is_compact_digest_with_ref() -> None:
    """PRD 0009 P3: a successful projected tool result enters the transcript as a
    COMPACT digest + a ``result_ref`` — never the full blob. The Gmail body
    (``bodyPreview``) must not reach the transcript (0056 + context saving)."""

    store = _make_store()
    task_id = _make_running_task(store)

    mail = _valid_mail_props()  # carries bodyPreview

    async def _handler(_ctx: Any, _args: Any) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(
            status="ok",
            result={"query": "label:INBOX", "count": 1, "messages": [mail]},
        )

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description="mail",
                args_model=GmailSearchArgs,
                handler=_handler,
                result_projector=project_gmail_search,
            )
        ]
    )

    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1}),
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
    assert body["tool"] == "gmail_search"
    assert body["status"] == "ok"
    assert body["result_ref"] == "gmail_search#1"
    assert body["result"]["count"] == 1
    # The body NEVER enters the transcript — the decisive 0056 + D2 assertion.
    assert "bodyPreview" not in tool_msgs[0].content
    assert "deck ready by Thursday" not in tool_msgs[0].content
    assert body["result"]["messages"][0] == {
        "subject": "Q3 forecast",
        "receivedAt": "2026-05-28T14:22:00Z",
        "from": "Marie Lefèvre",
    }


@pytest.mark.asyncio
async def test_p3_unprojected_tool_keeps_full_result_in_transcript() -> None:
    """PRD 0009 P3: a tool with no projector behaves as pre-0009 — its full
    result is preserved in the transcript (digest == full result) — and gains a
    ``result_ref`` handle. No regression for un-projected tools."""

    store = _make_store()
    task_id = _make_running_task(store)

    async def _handler(_ctx: Any, _args: Any) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(
            status="ok", result={"hits": ["a", "b"], "meta": {"k": "v"}}
        )

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="web_search",
                version="v1",
                description="search",
                args_model=WebSearchArgs,
                handler=_handler,  # no result_projector
            )
        ]
    )

    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("web_search", {"query": "x"}),
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
    # Full result preserved verbatim (digest == result for an un-projected tool).
    assert body["result"] == {"hits": ["a", "b"], "meta": {"k": "v"}}
    # But it still gets a ref handle on the blackboard.
    assert body["result_ref"] == "web_search#1"


# ---------------------------------------------------------------------------
# PRD 0009 P4 — deterministic terminal deliverable from the store
# ---------------------------------------------------------------------------


def _make_gmail_projector_registry(
    result: dict[str, Any],
) -> tuple[SubAgentToolRegistry, list[Any]]:
    """A ``gmail_search`` tool wired with the real projector, returning ``result``."""

    handler_calls: list[Any] = []

    async def _handler(_ctx: Any, args: Any) -> SubAgentToolHandlerOutcome:
        handler_calls.append(args)
        return SubAgentToolHandlerOutcome(status="ok", result=result)

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description="mail",
                args_model=GmailSearchArgs,
                handler=_handler,
                result_projector=project_gmail_search,
            )
        ]
    )
    return registry, handler_calls


def _no_converge_policy(**overrides: Any) -> SubAgentPolicy:
    """Policy with convergence OFF so a terminal result still reaches the
    stall / cap / model-done paths these tests exercise (P5 adds convergence)."""

    base: dict[str, Any] = {
        "max_iterations": 99,
        "wall_clock_seconds": 999.0,
        "token_cap": 10_000_000,
        "converge_on_terminal_result": False,
    }
    base.update(overrides)
    return SubAgentPolicy(**base)


@pytest.mark.asyncio
async def test_p4_stall_after_gmail_search_keeps_mail_deliverable() -> None:
    """PRD 0009 P4 — THE decisive 2026-05-30 regression. A stall AFTER a
    successful gmail_search force-terminates ``degraded``, and the Mail card is
    rebuilt from the stored result, so ``task.result_payload`` is the descriptor
    — NOT ``None`` (the empty-overlay bug). The model never emitted a ``done``."""

    mail = _valid_mail_props()
    result = {"query": "label:INBOX", "count": 1, "messages": [mail]}
    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, handler_calls = _make_gmail_projector_registry(result)
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1}),
            _progress_payload("j'attends la réponse de l'outil"),
            _progress_payload("j'attends la réponse de l'outil"),
            _progress_payload("j'attends la réponse de l'outil"),
            _progress_payload("j'attends la réponse de l'outil"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=_no_converge_policy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert state_changes[-1]["reason_code"] == REASON_STALLED
    # THE FIX: the structured Mail card survives the forced stall.
    assert task.result_payload == [{"component": "Mail", "props": mail}]
    assert handler_calls  # the search did run


@pytest.mark.asyncio
async def test_p4_iteration_cap_after_gmail_search_keeps_mail_deliverable() -> None:
    """PRD 0009 P4 — the cap paths also rebuild the deliverable from the store,
    not just the salvaged text. A degraded iteration-cap exit carries the Mail
    card."""

    mail = _valid_mail_props()
    result = {"query": "label:INBOX", "count": 1, "messages": [mail]}
    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, _ = _make_gmail_projector_registry(result)
    client = _ScriptedClient(
        chat_values=[_tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1})]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        # cap fires on the loop turn after the single tool_call increments iteration.
        policy=_no_converge_policy(max_iterations=1),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert state_changes[-1]["reason_code"] == REASON_ITERATION_CAP
    assert task.result_payload == [{"component": "Mail", "props": mail}]


@pytest.mark.asyncio
async def test_p4_done_by_result_ref_builds_card_from_store() -> None:
    """PRD 0009 P4 — the model finishes by REFERENCING the stored result
    (result_ref) and emits NO ui_payload; the runner builds the Mail card from
    the store. This is the "pass the data id" path — the model never copies the
    descriptor."""

    mail = _valid_mail_props()
    result = {"query": "label:INBOX", "count": 1, "messages": [mail]}
    store = _make_store()
    task_id = _make_running_task(store)
    registry, _ = _make_gmail_projector_registry(result)
    done_with_ref = json.dumps(
        {
            "action": "done",
            "result_summary": "Voici le dernier mail.",
            "status": "complete",
            "reason_code": "ok",
            "result_ref": "gmail_search#1",
        }
    )
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1}),
            done_with_ref,
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=_no_converge_policy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result_payload == [{"component": "Mail", "props": mail}]
    # The model's own prose summary is kept (not overwritten by the projection).
    assert task.result == "Voici le dernier mail."


@pytest.mark.asyncio
async def test_p4_single_result_ref_expands_to_full_multi_mail_section_list() -> None:
    """PRD 0010 / issue 0067 — a single ``result_ref`` in ``done`` expands to the
    FULL projected section list: one Mail section per returned message, in order.
    The model references one stored result and never enumerates the messages."""

    mails = [
        {**_valid_mail_props(), "subject": "mail A", "messageId": "m-a", "threadId": "t-a"},
        {**_valid_mail_props(), "subject": "mail B", "messageId": "m-b", "threadId": "t-b"},
        {**_valid_mail_props(), "subject": "mail C", "messageId": "m-c", "threadId": "t-c"},
    ]
    result = {"query": "label:INBOX", "count": 3, "messages": mails}
    store = _make_store()
    task_id = _make_running_task(store)
    registry, _ = _make_gmail_projector_registry(result)
    done_with_ref = json.dumps(
        {
            "action": "done",
            "result_summary": "Voici tes 3 derniers mails.",
            "status": "complete",
            "reason_code": "ok",
            "result_ref": "gmail_search#1",
        }
    )
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 3}),
            done_with_ref,
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=_no_converge_policy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result_payload is not None
    assert [s["component"] for s in task.result_payload] == ["Mail", "Mail", "Mail"]
    assert [s["props"]["subject"] for s in task.result_payload] == ["mail A", "mail B", "mail C"]


@pytest.mark.asyncio
async def test_p4_bare_done_with_stored_result_builds_card() -> None:
    """PRD 0009 P4 — the 2026-05-30 RC1 case: the model finally emits a BARE
    ``done`` (no ui_payload, no result_ref) with a usable result on the
    blackboard. The runner still rebuilds the card from the last stored
    result."""

    mail = _valid_mail_props()
    result = {"query": "label:INBOX", "count": 1, "messages": [mail]}
    store = _make_store()
    task_id = _make_running_task(store)
    registry, _ = _make_gmail_projector_registry(result)
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1}),
            _done_v2_payload(result_summary="fini", status="complete", reason_code="ok"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=_no_converge_policy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result_payload == [{"component": "Mail", "props": mail}]


@pytest.mark.asyncio
async def test_p4_model_authored_descriptor_is_respected_over_store() -> None:
    """PRD 0009 P4 — a model that DOES hand-build a valid descriptor keeps
    authority (precedence b): its descriptor is used, not the store fallback.
    Model agency is preserved for the cases where it wants it."""

    stored_mail = _valid_mail_props()
    result = {"query": "label:INBOX", "count": 1, "messages": [stored_mail]}
    authored = dict(_valid_mail_props())
    authored["subject"] = "Model-authored subject"
    store = _make_store()
    task_id = _make_running_task(store)
    registry, _ = _make_gmail_projector_registry(result)
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1}),
            _done_v2_payload(
                result_summary="fini",
                status="complete",
                reason_code="ok",
                ui_payload={"component": "Mail", "props": authored},
            ),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=_no_converge_policy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result_payload is not None
    props = task.result_payload[0]["props"]
    assert isinstance(props, dict)
    assert props["subject"] == "Model-authored subject"


# ---------------------------------------------------------------------------
# PRD 0009 P5 — convergence on a terminal tool result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p5_terminal_result_converges_immediately() -> None:
    """PRD 0009 P5 — a terminal projection (gmail_search count>0) finalises
    ``done(complete)`` deterministically right after dispatch: ONE LLM call, the
    Mail card built from the store, no progress/done round-trips. The weak model
    is removed from the happy path."""

    mail = _valid_mail_props()
    result = {"query": "label:INBOX", "count": 1, "messages": [mail]}
    store = _make_store()
    task_id = _make_running_task(store)
    bus = EventBus()
    state_changes = await _collect_state_changes(bus)
    registry, handler_calls = _make_gmail_projector_registry(result)
    client = _ScriptedClient(
        # Only the tool_call is scripted — convergence ends the run, so a second
        # scripted reply would be UNUSED (and _ScriptedClient only raises when it
        # runs OUT, so an unused tail is fine — but we provide none to prove it).
        chat_values=[_tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1})]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=bus,
        policy=SubAgentPolicy(),  # converge_on_terminal_result defaults True
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)
    for _ in range(5):
        await asyncio.sleep(0)

    assert handler_calls and len(handler_calls) == 1
    assert len(client.calls) == 1  # converged without a second LLM turn
    task = store.get_task(task_id)
    assert task.state == "done"
    assert state_changes[-1]["status"] == "complete"
    assert state_changes[-1]["reason_code"] == REASON_OK
    assert task.result_payload == [{"component": "Mail", "props": mail}]
    # The spoken summary is the deterministic projection, not empty.
    assert task.result and "1 email" in task.result


@pytest.mark.asyncio
async def test_p5_empty_terminal_result_converges_without_card() -> None:
    """PRD 0009 P5 — an empty search is ALSO terminal (nothing more to do): it
    converges to ``done(complete)`` with no card and the deterministic
    "aucun email" summary, in one LLM call."""

    result = {"query": "from:nobody", "count": 0, "messages": []}
    store = _make_store()
    task_id = _make_running_task(store)
    registry, _ = _make_gmail_projector_registry(result)
    client = _ScriptedClient(
        chat_values=[_tool_call_payload("gmail_search", {"from_email": "nobody@example.com"})]
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

    assert len(client.calls) == 1
    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result_payload == []
    assert task.result is not None
    assert "Aucun email" in task.result


@pytest.mark.asyncio
async def test_p5_convergence_disabled_waits_for_model_done() -> None:
    """PRD 0009 P5 — with convergence disabled, a terminal result does NOT
    short-circuit: the runner waits for the model's own ``done`` (two LLM
    calls). The flag is the operator / multi-step escape hatch."""

    mail = _valid_mail_props()
    result = {"query": "label:INBOX", "count": 1, "messages": [mail]}
    store = _make_store()
    task_id = _make_running_task(store)
    registry, _ = _make_gmail_projector_registry(result)
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1}),
            _done_v2_payload(result_summary="fini", status="complete", reason_code="ok"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=_no_converge_policy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    assert len(client.calls) == 2  # did NOT converge; consumed the model's done
    task = store.get_task(task_id)
    assert task.state == "done"
    # The bare done still got the card from the store (P4 precedence c).
    assert task.result_payload == [{"component": "Mail", "props": mail}]


@pytest.mark.asyncio
async def test_p5_nonterminal_tool_does_not_converge() -> None:
    """PRD 0009 P5 — a tool whose projection is NOT terminal (the default
    projector: any un-projected tool) does not converge; the runner waits for
    the model to conclude."""

    store = _make_store()
    task_id = _make_running_task(store)
    registry, _ = _make_echo_registry()  # no projector → terminal=False
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("echo", {"q": "x"}),
            _done_v2_payload(result_summary="done", status="complete", reason_code="ok"),
        ]
    )
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),  # converge default True, but echo is non-terminal
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    assert len(client.calls) == 2  # waited for the model's done
    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "done"


# ---------------------------------------------------------------------------
# PRD 0009 P6 — prompt: result_ref guidance + slimmed Gmail skill pack
# ---------------------------------------------------------------------------


def test_p6_base_prompt_teaches_result_ref_without_naming_a_tool() -> None:
    """PRD 0009 P6 — the base contract teaches the ``result_ref`` finishing path
    (reference a stored result instead of copying it into ui_payload), with a
    GENERIC example so it stays tool-agnostic."""

    from bob.context.prompt_fragments import SUB_AGENT_V2_SYSTEM_PROMPT

    rendered = SUB_AGENT_V2_SYSTEM_PROMPT.render(goal="dummy")
    assert "result_ref" in rendered
    assert "outil#1" in rendered  # generic example
    # Stays tool-agnostic — no specific tool leaks into the base contract.
    assert "gmail_search" not in rendered


def test_p6_gmail_pack_drops_hand_built_descriptor_and_empty_branch() -> None:
    """PRD 0009 P6 — the slimmed Gmail pack no longer asks the model to
    hand-build the Mail descriptor or handle the empty result (the runner does
    both via convergence), but keeps the search filters/fallback and the
    tool-ERROR branches."""

    from bob.context.prompt_fragments import GMAIL_SEARCH_SKILL_PACK

    rendered = GMAIL_SEARCH_SKILL_PACK.render()
    # Happy-path descriptor construction is gone (now automatic).
    assert '{"component": "Mail"' not in rendered
    assert "lecture du mail" not in rendered
    # The mail-build no longer asks for a count:0 model speech.
    assert "Aucun mail récent" not in rendered
    # Kept: search construction + fallback + result_ref mention + error speeches.
    assert 'label="INBOX"' in rendered
    assert 'label="SENT"' in rendered
    assert "result_ref" in rendered
    assert "python -m bob.connectors.gmail.auth" in rendered


# ---------------------------------------------------------------------------
# PRD 0009 P9 — review hardening (deliverable precedence + projector safety)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p9_resolved_result_ref_to_empty_does_not_ship_other_card() -> None:
    """PRD 0009 P9 — a RESOLVED result_ref is authoritative. If the model
    references a stored result that has NO card (an empty search), the runner
    must NOT substitute a different (later) tool result's card via last(). The
    answer is "no card", honouring the model's explicit choice."""

    mail = _valid_mail_props()
    calls = {"n": 0}

    async def _handler(_ctx: Any, _args: Any) -> SubAgentToolHandlerOutcome:
        calls["n"] += 1
        if calls["n"] == 1:
            # First call: empty result → gmail_search#1 has no deliverable.
            return SubAgentToolHandlerOutcome(
                status="ok", result={"query": "from:nobody", "count": 0, "messages": []}
            )
        # Second call: a card → gmail_search#2 (this becomes last()).
        return SubAgentToolHandlerOutcome(
            status="ok", result={"query": "label:INBOX", "count": 1, "messages": [mail]}
        )

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description="mail",
                args_model=GmailSearchArgs,
                handler=_handler,
                result_projector=project_gmail_search,
            )
        ]
    )
    client = _ScriptedClient(
        chat_values=[
            _tool_call_payload("gmail_search", {"from_email": "nobody@example.com"}),
            _tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1}),
            # The model references the EMPTY result #1, not the card #2.
            json.dumps(
                {
                    "action": "done",
                    "result_summary": "rien",
                    "status": "complete",
                    "reason_code": "ok",
                    "result_ref": "gmail_search#1",
                }
            ),
        ]
    )
    store = _make_store()
    task_id = _make_running_task(store)
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=_no_converge_policy(),
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    # The referenced result had no card → no card shipped (NOT #2's card).
    assert task.result_payload == []


@pytest.mark.asyncio
async def test_p9_invalid_projector_card_is_dropped_keeping_text() -> None:
    """PRD 0009 P9 — defensive guard: the convergence path builds the card from
    the projector and bypasses the up-front deliverable validation. A projector
    that emits an INVALID card (missing required Mail props) must not reach the
    frontend: the runner drops the structured payload and keeps the text."""

    from bob.sub_agent.result_store import ProjectedResult

    def _bad_projector(_result: dict[str, Any]) -> ProjectedResult:
        return ProjectedResult(
            digest={"count": 1},
            # Invalid Mail props (missing from/receivedAt/threadId/… ).
            deliverable=[{"component": "Mail", "props": {"subject": "only a subject"}}],
            summary="un mail trouvé",
            terminal=True,
        )

    async def _handler(_ctx: Any, _args: Any) -> SubAgentToolHandlerOutcome:
        return SubAgentToolHandlerOutcome(status="ok", result={"count": 1})

    registry = SubAgentToolRegistry(
        [
            SubAgentToolDefinition(
                name="gmail_search",
                version="v1",
                description="mail",
                args_model=GmailSearchArgs,
                handler=_handler,
                result_projector=_bad_projector,
            )
        ]
    )
    client = _ScriptedClient(
        chat_values=[_tool_call_payload("gmail_search", {"label": "INBOX", "max_results": 1})]
    )
    store = _make_store()
    task_id = _make_running_task(store)
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(),  # converge ON: builds the (bad) card from the projector
        tool_registry=registry,
        clock=_ControllableClock(),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    # The invalid card was dropped — the frontend never receives bad props …
    assert task.result_payload == []
    # … but the deterministic text summary survives.
    assert task.result == "un mail trouvé"


# --- PRD 0010 / issue 0066 — sections-list pipeline unit tests ---------------


def test_model_payload_to_sections_markdown_string_becomes_one_markdown_section() -> None:
    """A document-class markdown string lifts onto a list-of-one Markdown section."""

    sections = _model_payload_to_sections("# Titre\n\ncorps")
    assert sections == [{"component": "Markdown", "props": {"content": "# Titre\n\ncorps"}}]


def test_model_payload_to_sections_descriptor_becomes_one_section() -> None:
    """A hand-built ``{component, props}`` descriptor becomes a list-of-one."""

    descriptor = {"component": "Mail", "props": {"messageId": "m"}}
    assert _model_payload_to_sections(descriptor) == [descriptor]


def test_model_payload_to_sections_returns_none_for_empty_or_unrenderable() -> None:
    """Nothing renderable → ``None`` so the caller falls through to the store path."""

    assert _model_payload_to_sections(None) is None
    assert _model_payload_to_sections("") is None
    assert _model_payload_to_sections("   ") is None
    # A bag with no renderable text key and no component discriminator.
    assert _model_payload_to_sections({"foo": "bar"}) is None


def test_resolve_terminal_deliverable_returns_section_list_from_store() -> None:
    """The resolver returns the projection's section list + summary (PRD 0010)."""

    from bob.sub_agent.result_store import ProjectedResult, ToolResultStore

    def _projector(_result: dict[str, Any]) -> ProjectedResult:
        return ProjectedResult(
            deliverable=[{"component": "Mail", "props": {"messageId": "m"}}],
            summary="un mail",
            terminal=True,
        )

    store = ToolResultStore()
    store.put(
        tool_name="gmail_search",
        tool_version="v1",
        result={"count": 1},
        projector=_projector,
    )
    sections, summary = _resolve_terminal_deliverable(store)
    assert sections == [{"component": "Mail", "props": {"messageId": "m"}}]
    assert summary == "un mail"


def test_resolve_terminal_deliverable_none_when_store_empty() -> None:
    """No stored result → ``(None, None)`` so a terminal exit ships no card."""

    from bob.sub_agent.result_store import ToolResultStore

    sections, summary = _resolve_terminal_deliverable(ToolResultStore())
    assert sections is None
    assert summary is None
