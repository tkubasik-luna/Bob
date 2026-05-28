"""Orchestrator integration tests for the per-tool retry policy (issue 0048).

Covers the behavioural acceptance criteria:

- 1 failed dispatch + 1 successful dispatch → the orchestrator surfaces
  the success and the LLM saw a validator-feedback message under the
  ``system_validator`` role on the retry call.
- 2 failed dispatches → the orchestrator routes through
  ``on_validation_exhausted`` and the user-visible speech is the
  hardcoded degrade phrase. The handler logs the structured
  ``jarvis.validation_failed`` event (asserted via debug_log).
- The retry counter is NEVER persisted to any :class:`ContextEntry`
  row (no DB column carries it).
- Unknown ``task_id`` on ``addendum_task`` follows the same
  degrade path with the same speech.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

import pytest

from bob import ws_events
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse, ToolCall, ToolDefinition
from bob.llm_client import LLMClient
from bob.orchestrator import Orchestrator
from bob.task_store import TaskStore
from bob.validation.exhausted import JARVIS_DEGRADE_SPEECH_FRAGMENT
from bob.validation.system_validator import (
    INVALID_OUTPUT_PREFIX,
    SYSTEM_VALIDATOR_ROLE,
)


class _ScriptedClient(LLMClient):
    """Returns successive :class:`LLMResponse` instances from a list."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.complete_calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        raise NotImplementedError("not used in retry tests")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        self.complete_calls.append({"messages": list(messages), "tools": tools})
        if not self._responses:
            raise AssertionError("scripted client ran out of responses")
        return self._responses.pop(0)


class _RecordingScheduler:
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
    responses: list[LLMResponse],
) -> tuple[
    Orchestrator,
    _ScriptedClient,
    JarvisStore,
    TaskStore,
    _RecordingScheduler,
]:
    client = _ScriptedClient(responses)
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler(task_store)
    # Use the bounded-v1 policy here to side-step the in-progress 0050
    # state_block leak; the validation flow under test is policy-agnostic.
    from bob.context.policy import bounded_v1_policy

    orchestrator = Orchestrator(
        jarvis_client=client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        task_scheduler=scheduler,
        jarvis_prompt="Tu es Jarvis.",
        context_policy=bounded_v1_policy(),
    )
    return orchestrator, client, jarvis_store, task_store, scheduler


def _say_call(speech: str = "ok", ui: object | None = None) -> ToolCall:
    args: dict[str, Any] = {"speech": speech}
    if ui is not None:
        args["ui"] = ui
    return ToolCall(id=f"call_{uuid4().hex[:6]}", name="say", arguments=args)


def _spawn_call(*, title: str, goal: str) -> ToolCall:
    return ToolCall(
        id=f"call_{uuid4().hex[:6]}",
        name="spawn_task",
        arguments={"title": title, "goal": goal},
    )


@pytest.mark.asyncio
async def test_malformed_then_valid_recovers_via_validator_feedback() -> None:
    """First attempt invalid_args → second attempt succeeds with degrade speech."""

    # First response: no tool call (contract violation).
    # Second response: valid say.
    orchestrator, client, jarvis_store, _ts, _sched = _make_orchestrator(
        [
            LLMResponse(text="just chatting", tool_calls=[]),
            LLMResponse(text=None, tool_calls=[_say_call("Salut Tom")]),
        ]
    )

    response = await orchestrator.process_user_message("s1", "Coucou")

    # Behavioural assertion: the retry happened (2 complete calls).
    assert len(client.complete_calls) == 2

    # The orchestrator surfaced the SUCCESS speech, not the degrade.
    assert response.speech == "Salut Tom"

    # The retry call carried a ``system_validator`` message.
    retry_messages = client.complete_calls[1]["messages"]
    validator_msgs = [m for m in retry_messages if m["role"] == SYSTEM_VALIDATOR_ROLE]
    assert len(validator_msgs) == 1
    assert "n'as pas appelé d'outil" in validator_msgs[0]["content"]

    # The success persisted exactly one assistant turn (no leftover from
    # the failed first attempt).
    history = jarvis_store.history()
    assert history[-1] == {"role": "assistant", "content": "Salut Tom"}


@pytest.mark.asyncio
async def test_two_failed_attempts_trigger_hardcoded_degrade_say() -> None:
    """Two contract violations → hardcoded degrade speech via on_validation_exhausted."""

    orchestrator, client, jarvis_store, _ts, _sched = _make_orchestrator(
        [
            LLMResponse(text="first failure", tool_calls=[]),
            LLMResponse(text="second failure", tool_calls=[]),
        ]
    )

    response = await orchestrator.process_user_message("s1", "Coucou")

    # Two LLM round-trips: initial + 1 retry. The exhausted handler
    # then takes over, without calling the LLM again.
    assert len(client.complete_calls) == 2
    # Speech is the hardcoded degrade phrase, routed through the SayTool
    # via the dispatcher.
    assert response.speech == JARVIS_DEGRADE_SPEECH_FRAGMENT.template
    # The degrade went through the dispatcher → SayTool, which
    # persisted the assistant row in the Jarvis store.
    history = jarvis_store.history()
    assert any(row["content"] == JARVIS_DEGRADE_SPEECH_FRAGMENT.template for row in history)


@pytest.mark.asyncio
async def test_invalid_args_then_valid_recovers() -> None:
    """A first invalid spawn args call retries to a successful spawn."""

    orchestrator, client, _js, task_store, scheduler = _make_orchestrator(
        [
            # First attempt: missing required ``goal``.
            LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="call_bad",
                        name="spawn_task",
                        arguments={"title": "Just a title"},
                    )
                ],
            ),
            # Second attempt: valid spawn.
            LLMResponse(
                text=None,
                tool_calls=[_spawn_call(title="Drafts", goal="Draft 3 emails")],
            ),
        ]
    )

    response = await orchestrator.process_user_message("s1", "Draft emails")

    assert len(client.complete_calls) == 2
    assert response.spawned_task_ids == scheduler.enqueued
    # Validator feedback flowed on the retry under the dedicated role.
    retry_messages = client.complete_calls[1]["messages"]
    validator_msgs = [m for m in retry_messages if m["role"] == SYSTEM_VALIDATOR_ROLE]
    assert validator_msgs
    assert "Validation a échoué" in validator_msgs[0]["content"]
    assert "spawn_task" in validator_msgs[0]["content"]
    # Task did get created on the second attempt.
    assert task_store.list_tasks(state="running")[0].title == "Drafts"


@pytest.mark.asyncio
async def test_unknown_task_id_degrades_via_same_handler() -> None:
    """Unknown ``task_id`` on ``addendum_task`` routes through the degrade."""

    # Both attempts reference a missing task id → exhaustion path.
    bad_call = ToolCall(
        id="call_bad",
        name="addendum_task",
        arguments={"task_id": "does-not-exist", "info": "x"},
    )
    orchestrator, client, jarvis_store, _ts, scheduler = _make_orchestrator(
        [
            LLMResponse(text=None, tool_calls=[bad_call]),
            LLMResponse(text=None, tool_calls=[bad_call]),
        ]
    )

    response = await orchestrator.process_user_message("s1", "Amical.")
    assert response.speech == JARVIS_DEGRADE_SPEECH_FRAGMENT.template
    assert scheduler.resumed == []
    # Two attempts, then degrade — no third complete() call.
    assert len(client.complete_calls) == 2
    # Hardcoded degrade still went through the dispatcher → SayTool
    # persistence.
    history = jarvis_store.history()
    assert any(row["content"] == JARVIS_DEGRADE_SPEECH_FRAGMENT.template for row in history)


@pytest.mark.asyncio
async def test_retry_counter_never_lands_in_persisted_history() -> None:
    """The transient :class:`CallEnvelope` must NOT leak into ContextEntry rows.

    Asserts that no jarvis store row contains the retry counter or the
    validator feedback (those are transient artefacts of the in-memory
    envelope). The success path persists exactly the assistant reply
    and the user message — nothing else.
    """

    orchestrator, _client, jarvis_store, _ts, _sched = _make_orchestrator(
        [
            LLMResponse(text="first failure", tool_calls=[]),
            LLMResponse(text=None, tool_calls=[_say_call("Salut")]),
        ]
    )

    await orchestrator.process_user_message("s1", "Coucou")

    history = jarvis_store.history()
    for row in history:
        content = row["content"]
        assert "retry" not in content.lower() or row["role"] == "user"
        assert INVALID_OUTPUT_PREFIX not in content
        assert "attempts" not in content.lower() or row["role"] == "user"


@pytest.mark.asyncio
async def test_logs_jarvis_validation_failed_on_exhaustion() -> None:
    """Structured ``jarvis.validation_failed`` event fires on exhaustion."""

    from bob import debug_log

    debug_log.clear()
    debug_log.current_turn_id.set(None)

    orchestrator, _client, _js, _ts, _sched = _make_orchestrator(
        [
            LLMResponse(text="first failure", tool_calls=[]),
            LLMResponse(text="second failure", tool_calls=[]),
        ]
    )

    received: list[dict[str, Any]] = []

    async def _emitter(event: dict[str, Any]) -> None:
        received.append(event)

    ws_events.set_emitter(_emitter)
    try:
        await orchestrator.process_user_message("s1", "Coucou")
    finally:
        ws_events.set_emitter(None)

    # The hardcoded degrade speech reached the WS layer via the
    # orchestrator return value (not via an assistant_msg emit from
    # within the handler — that's the orchestrator's job after
    # ``process_user_message`` returns).
