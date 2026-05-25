"""Integration test wiring :class:`ContextAssembler` + :class:`FakeLLMClient` + Orchestrator.

This is the smallest contract test that proves the end-to-end flow defined
by issue 0043: the orchestrator no longer reads ``jarvis_store`` directly,
it goes through the assembler and the legacy provider. The test asserts
that the messages list reaching :meth:`FakeLLMClient.complete` matches the
pre-0043 inline construction byte-for-byte.

Later slices (0044-0052) will reuse this same harness to assert the
streaming + tool-dispatch contracts evolve correctly.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bob.context.policy import legacy_full_history_policy
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse
from bob.orchestrator import _TOOLS_SYSTEM_ADDENDUM, Orchestrator
from bob.task_store import TaskStore

from ._harness.fake_llm import FakeLLMClient

_TEST_JARVIS_PROMPT = "Tu es Jarvis-de-test."


class _RecordingScheduler:
    """No-op scheduler that records calls."""

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
    fake_client: FakeLLMClient,
) -> tuple[Orchestrator, JarvisStore, TaskStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    jarvis_store = JarvisStore(conn)
    task_store = TaskStore(conn)
    scheduler = _RecordingScheduler(task_store)
    orchestrator = Orchestrator(
        jarvis_client=fake_client,
        jarvis_store=jarvis_store,
        task_store=task_store,
        task_scheduler=scheduler,
        jarvis_prompt=_TEST_JARVIS_PROMPT,
        context_policy=legacy_full_history_policy(),
    )
    return orchestrator, jarvis_store, task_store


@pytest.mark.asyncio
async def test_orchestrator_uses_assembler_for_complete_call() -> None:
    """The ``complete()`` call reaches the LLM via the assembler.

    The messages list must contain a single system message and the full
    persisted history including the user turn just appended — exactly what
    the pre-0043 orchestrator did inline.
    """

    fake = FakeLLMClient(
        complete_responses=[LLMResponse(text="ok", tool_calls=[])],
        chat_responses=[json.dumps({"speech": "Bonjour", "ui": []})],
    )
    orchestrator, jarvis_store, _ = _make_orchestrator(fake)

    # Seed two prior turns so we can prove the assembler returns the full
    # thread.
    jarvis_store.append("user", "première question")
    jarvis_store.append("assistant", "première réponse")

    await orchestrator.process_user_message("s1", "deuxième question")

    # ``complete()`` was called once with the full assembled messages list.
    assert len(fake.complete_calls) == 1
    messages = fake.complete_calls[0]["messages"]

    # First message is the system prompt with the tools addendum baked in.
    assert messages[0]["role"] == "system"
    assert _TEST_JARVIS_PROMPT in messages[0]["content"]
    assert _TOOLS_SYSTEM_ADDENDUM in messages[0]["content"]

    # Then the persisted history in order, plus the live user turn appended
    # just before the assembler ran.
    bodies = [(m["role"], m["content"]) for m in messages[1:]]
    assert bodies == [
        ("user", "première question"),
        ("assistant", "première réponse"),
        ("user", "deuxième question"),
    ]


@pytest.mark.asyncio
async def test_structured_chat_path_uses_assembler_without_tools_addendum() -> None:
    """The structured ``chat()`` call drops the tools addendum but keeps history."""

    fake = FakeLLMClient(
        complete_responses=[LLMResponse(text="just chatting", tool_calls=[])],
        chat_responses=[json.dumps({"speech": "ok", "ui": []})],
    )
    orchestrator, jarvis_store, _ = _make_orchestrator(fake)

    jarvis_store.append("user", "prior")
    jarvis_store.append("assistant", "ack")

    await orchestrator.process_user_message("s1", "new turn")

    assert len(fake.chat_calls) == 1
    chat_msgs = fake.chat_calls[0]["messages"]

    # System message must NOT include the tools-system addendum on the
    # structured path.
    assert chat_msgs[0]["role"] == "system"
    assert _TEST_JARVIS_PROMPT in chat_msgs[0]["content"]
    assert _TOOLS_SYSTEM_ADDENDUM not in chat_msgs[0]["content"]
    assert "spawn_subtask" not in chat_msgs[0]["content"]

    # History flows through unchanged.
    bodies = [(m["role"], m["content"]) for m in chat_msgs[1:]]
    assert bodies == [
        ("user", "prior"),
        ("assistant", "ack"),
        ("user", "new turn"),
    ]


@pytest.mark.asyncio
async def test_fake_llm_runs_out_of_responses_raises_assertion() -> None:
    """Sanity guard on the test harness — under-scripted calls fail loudly."""

    fake = FakeLLMClient(complete_responses=[])
    orchestrator, _js, _ts = _make_orchestrator(fake)

    with pytest.raises(AssertionError):
        await orchestrator.process_user_message("s1", "x")
