"""Stable prompt prefix — PRD 0018 / issue 0128.

Local-model prefix caching (LM Studio KV-cache) only pays off when the head
of the prompt is byte-identical from one LLM call to the next. These tests
pin that EXTERNAL contract on both prompt assemblies:

- **Sub-agent runner** — the advertised-tools selection (``select_tools``)
  and the rendered tool catalogue are per-run-immutable: on a multi-iteration
  run every iteration carries the byte-identical stable prefix (base contract
  + skill packs + catalogue), with the variable temporal context trailing it.
  Validator feedback injected on retry lands AFTER the assembled messages and
  never perturbs the system block.
- **Jarvis orchestrator** — the system-prompt prefix (personality + UI
  addendum + tools contract) is byte-identical between two consecutive turns
  of the same session; the variable fragments (temporal context, waiting-input
  list) trail it.

Per the issue's testing decision the assertions compare assembled PROMPT
CONTENT across iterations / turns — never internal call counts. The temporal
fragment is monkeypatched to return a DIFFERENT marker on every call, which
simulates a run/session spanning midnight: if any stable fragment sat after
the temporal one (the pre-0128 layout), the prefix comparison would fail.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from pydantic import BaseModel

import bob.orchestrator as orchestrator_mod
import bob.sub_agent.runner as runner_mod
from bob.context.policy import legacy_full_history_policy
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import LLMClient
from bob.orchestrator import _TOOLS_SYSTEM_ADDENDUM, Orchestrator
from bob.sub_agent.policy import SubAgentPolicy
from bob.sub_agent.runner import SubAgentRunner
from bob.sub_agent.tool_registry import (
    SubAgentToolDefinition,
    SubAgentToolHandlerOutcome,
    SubAgentToolRegistry,
)
from bob.task_store import TaskStore
from bob.validation.system_validator import SYSTEM_VALIDATOR_ROLE

from ._harness.fake_llm import FakeLLMClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_task_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _make_running_task(store: TaskStore, *, goal: str) -> str:
    task_id = store.create_task(title="t", goal=goal)
    store.update_state(task_id, "running")
    return task_id


def _sequenced_temporal(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    """Patch ``module.temporal_context_fragment`` to a per-call marker.

    Returns the (mutating) list of markers emitted so far, so the test can map
    LLM call *i* to marker ``markers[i]``. A fresh marker per call simulates a
    run / session spanning midnight — the strongest realistic perturbation of
    the variable temporal fragment.
    """

    markers: list[str] = []

    def _fake(now: Any | None = None) -> str:
        marker = f"CONTEXTE-TEMPOREL-{len(markers) + 1}"
        markers.append(marker)
        return marker

    monkeypatch.setattr(module, "temporal_context_fragment", _fake)
    return markers


# ---------------------------------------------------------------------------
# Sub-agent runner
# ---------------------------------------------------------------------------


class _ScriptedClient(LLMClient):
    """Scripted ``chat()`` client recording every call's messages."""

    def __init__(
        self,
        *,
        chat_values: list[str],
        chat_callbacks: list[Any] | None = None,
    ) -> None:
        self._chat_values = list(chat_values)
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


class _MailArgs(BaseModel):
    label: str


async def _ok(_ctx: Any, _args: BaseModel) -> SubAgentToolHandlerOutcome:
    return SubAgentToolHandlerOutcome(status="ok", result={})


def _mail_tool(name: str = "gmail_search") -> SubAgentToolDefinition:
    return SubAgentToolDefinition(
        name=name,
        version="v1",
        description="Recherche dans la boîte Gmail.",
        args_model=_MailArgs,
        handler=_ok,
        tags=("mail", "email", "gmail", "inbox"),
    )


def _make_runner(client: LLMClient, registry: SubAgentToolRegistry) -> SubAgentRunner:
    return SubAgentRunner(
        subagent_client=client,
        task_store=_make_task_store(),
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=8),
        tool_registry=registry,
    )


def _progress(thought: str) -> str:
    return json.dumps({"action": "progress", "thought": thought})


_DONE = json.dumps(
    {
        "action": "done",
        "result_summary": "fini",
        "status": "complete",
        "reason_code": "ok",
        "cost": {},
    }
)


def _system_prefix(message: dict[str, Any], marker: str) -> str:
    """The system content with its trailing variable temporal block stripped."""

    content: str = message["content"]
    assert message["role"] == "system"
    assert content.endswith("\n\n" + marker), (
        "temporal context must be the trailing (variable) fragment of the system prompt"
    )
    return content.removesuffix("\n\n" + marker)


@pytest.mark.asyncio
async def test_runner_stable_prefix_byte_identical_across_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three iterations → three byte-identical stable prefixes, catalogue included.

    The temporal fragment changes on EVERY call (simulated midnight flips), so
    the equality below holds only if all stable fragments (base contract +
    skill packs + tool catalogue) sit BEFORE the variable one.
    """

    markers = _sequenced_temporal(monkeypatch, runner_mod)
    client = _ScriptedClient(chat_values=[_progress("a"), _progress("b"), _DONE])
    runner = _make_runner(client, SubAgentToolRegistry([_mail_tool()]))
    task_id = _make_running_task(runner._task_store, goal="Trouve le dernier mail de Paul")

    await runner.run(task_id)

    assert len(client.calls) == 3
    prefixes = [
        _system_prefix(call["messages"][0], marker)
        for call, marker in zip(client.calls, markers, strict=True)
    ]
    # The whole stable prefix — including the rendered tool catalogue — is
    # byte-identical at every iteration.
    assert prefixes[0] == prefixes[1] == prefixes[2]
    # No functional regression: the advertised tools are still present and the
    # catalogue still carries the real JSON Schema block...
    assert "Outils disponibles" in prefixes[0]
    assert "gmail_search" in prefixes[0]
    assert '"type": "object"' in prefixes[0]
    # ...and the (fresh) temporal context is still present on every iteration.
    for call, marker in zip(client.calls, markers, strict=True):
        assert marker in call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_runner_catalogue_frozen_for_the_run_despite_registry_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advertised catalogue is computed once per run (the goal is immutable).

    A tool registered mid-run — between iteration 1 and 2 — must NOT surface in
    iteration 2's prompt: the rendered block is reused byte-identical, never
    recomputed per iteration.
    """

    monkeypatch.setattr(runner_mod, "temporal_context_fragment", lambda now=None: "TEMPOREL-FIXE")
    registry = SubAgentToolRegistry([_mail_tool()])
    client = _ScriptedClient(
        chat_values=[_progress("a"), _DONE],
        # Fires inside iteration 1's chat() — i.e. after the first prompt was
        # built and before iteration 2's.
        chat_callbacks=[lambda: registry.register(_mail_tool(name="mail_extra_tool"))],
    )
    runner = _make_runner(client, registry)
    task_id = _make_running_task(runner._task_store, goal="Trouve le dernier mail de Paul")

    await runner.run(task_id)

    assert len(client.calls) == 2
    first = client.calls[0]["messages"][0]["content"]
    second = client.calls[1]["messages"][0]["content"]
    assert first == second
    assert "mail_extra_tool" not in second


@pytest.mark.asyncio
async def test_runner_validation_feedback_lands_after_stable_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry's validator feedback never alters the system block.

    Iteration 1 emits unparseable output; the runner re-injects feedback under
    the ``system_validator`` role. Iteration 2's SYSTEM message must stay
    byte-identical to iteration 1's, with the feedback appended at the very
    end of the message list.
    """

    monkeypatch.setattr(runner_mod, "temporal_context_fragment", lambda now=None: "TEMPOREL-FIXE")
    client = _ScriptedClient(chat_values=["pas du JSON du tout", _DONE])
    runner = _make_runner(client, SubAgentToolRegistry([_mail_tool()]))
    task_id = _make_running_task(runner._task_store, goal="Trouve le dernier mail de Paul")

    await runner.run(task_id)

    assert len(client.calls) == 2
    first_messages = client.calls[0]["messages"]
    retry_messages = client.calls[1]["messages"]
    # The stable prefix (the whole system block here) is untouched by the retry.
    assert retry_messages[0] == first_messages[0]
    # The feedback rides the dedicated role, strictly after every other message.
    assert retry_messages[-1]["role"] == SYSTEM_VALIDATOR_ROLE
    assert all(m["role"] != SYSTEM_VALIDATOR_ROLE for m in retry_messages[:-1])
    # And it never leaked into the system content.
    assert retry_messages[-1]["content"] not in retry_messages[0]["content"]


# ---------------------------------------------------------------------------
# Jarvis orchestrator
# ---------------------------------------------------------------------------

_TEST_JARVIS_PROMPT = "Tu es Jarvis-de-test."


class _RecordingScheduler:
    """No-op scheduler satisfying the orchestrator's dependency."""

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store

    async def enqueue(self, task_id: str) -> None:
        self._task_store.update_state(task_id, "running")

    async def resume(self, task_id: str) -> None:
        self._task_store.update_state(task_id, "running")

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        return None


def _say_response(speech: str = "ok") -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="call_say", name="say", arguments={"speech": speech})],
    )


def _make_orchestrator(
    fake_client: FakeLLMClient,
) -> tuple[Orchestrator, TaskStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    orchestrator = Orchestrator(
        jarvis_client=fake_client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        task_scheduler=_RecordingScheduler(task_store),
        jarvis_prompt=_TEST_JARVIS_PROMPT,
        context_policy=legacy_full_history_policy(),
    )
    return orchestrator, task_store


@pytest.mark.asyncio
async def test_jarvis_system_prefix_byte_identical_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive turns share a byte-identical system-prompt prefix.

    The temporal fragment differs between the turns (per-call marker), so the
    shared-prefix assertion holds only because every stable fragment
    (personality + UI addendum + tools contract) is ordered before it.
    """

    markers = _sequenced_temporal(monkeypatch, orchestrator_mod)
    fake = FakeLLMClient(complete_responses=[_say_response("un"), _say_response("deux")])
    orchestrator, _task_store = _make_orchestrator(fake)

    await orchestrator.process_user_message("s1", "première question")
    await orchestrator.process_user_message("s1", "deuxième question")

    assert len(fake.stream_calls) == 2
    first = fake.stream_calls[0]["messages"][0]
    second = fake.stream_calls[1]["messages"][0]
    assert first["role"] == "system"
    assert second["role"] == "system"
    # No waiting-input tasks → the temporal context is the trailing fragment.
    prefix_one = _system_prefix(first, markers[0])
    prefix_two = _system_prefix(second, markers[1])
    assert prefix_one == prefix_two
    # The stable prefix still carries everything the turn needs: personality,
    # UI addendum marker and the tools contract.
    assert _TEST_JARVIS_PROMPT in prefix_one
    assert _TOOLS_SYSTEM_ADDENDUM in prefix_one
    # The temporal context is present (fresh) on both turns, after the prefix.
    assert markers[0] in first["content"]
    assert markers[1] in second["content"]


@pytest.mark.asyncio
async def test_jarvis_waiting_input_addendum_trails_the_stable_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The waiting-input list (variable) lands after the temporal fragment.

    Both variable fragments trail the stable prefix: ``stable + temporal +
    waiting``. A turn with a waiting task therefore shares the same prefix as
    a turn without one.
    """

    markers = _sequenced_temporal(monkeypatch, orchestrator_mod)
    fake = FakeLLMClient(complete_responses=[_say_response("un"), _say_response("deux")])
    orchestrator, task_store = _make_orchestrator(fake)

    await orchestrator.process_user_message("s1", "première question")

    # A sub-task parks in ``waiting_input`` between the two turns.
    task_id = task_store.create_task(title="t-attente", goal="demander un détail")
    task_store.update_state(task_id, "running")
    task_store.update_state(task_id, "waiting_input")

    await orchestrator.process_user_message("s1", "deuxième question")

    first = fake.stream_calls[0]["messages"][0]["content"]
    second = fake.stream_calls[1]["messages"][0]["content"]
    # Turn 2 carries the waiting list strictly AFTER the temporal fragment.
    assert "Sous-tâches en attente" in second
    assert second.index(markers[1]) < second.index("Sous-tâches en attente")
    # The stable prefix (everything before the temporal marker) is unchanged
    # by the appearance of the waiting list.
    assert first.split(markers[0])[0] == second.split(markers[1])[0]
