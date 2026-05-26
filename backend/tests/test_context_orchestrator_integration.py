"""Integration test wiring :class:`ContextAssembler` + :class:`FakeLLMClient` + Orchestrator.

This is the smallest contract test that proves the end-to-end flow defined
by issue 0043: the orchestrator no longer reads ``jarvis_store`` directly,
it goes through the assembler and the legacy provider. The test asserts
that the messages list reaching :meth:`FakeLLMClient.stream_complete`
matches the pre-0043 inline construction byte-for-byte.

Issue 0047 unified Jarvis emission as tool calls.
Issue 0049 switched the orchestrator from ``complete()`` to
``stream_complete()`` so the user hears Jarvis speak while the LLM is
still generating. The assembler contract is unchanged; we just inspect
``stream_calls`` instead of ``complete_calls`` here.
"""

from __future__ import annotations

import sqlite3

import pytest

from bob.context.policy import legacy_full_history_policy
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.jarvis_store import JarvisStore
from bob.llm.types import LLMResponse, ToolCall
from bob.orchestrator import _TOOLS_SYSTEM_ADDENDUM, Orchestrator
from bob.task_store import TaskStore

from ._harness.fake_llm import FakeLLMClient


def _say_response(speech: str = "ok") -> LLMResponse:
    """Build a minimal ``say`` tool-call response (issue 0047)."""

    return LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(id="call_say", name="say", arguments={"speech": speech}),
        ],
    )


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
async def test_orchestrator_uses_assembler_for_stream_complete_call() -> None:
    """The streamed call reaches the LLM via the assembler.

    The messages list must contain a single system message and the full
    persisted history including the user turn just appended — exactly what
    the pre-0043 orchestrator did inline. Issue 0049 swapped ``complete()``
    for ``stream_complete()`` (with the fallback path replaying the same
    scripted :class:`LLMResponse` as a synthetic chunk trio), so we
    inspect ``stream_calls`` here.
    """

    fake = FakeLLMClient(complete_responses=[_say_response(speech="Bonjour")])
    orchestrator, jarvis_store, _ = _make_orchestrator(fake)

    # Seed two prior turns so we can prove the assembler returns the full
    # thread.
    jarvis_store.append("user", "première question")
    jarvis_store.append("assistant", "première réponse")

    await orchestrator.process_user_message("s1", "deuxième question")

    # ``stream_complete()`` was called once with the full assembled
    # messages list; the structured ``chat()`` path is gone (issue 0047).
    assert len(fake.stream_calls) == 1
    assert fake.chat_calls == []
    messages = fake.stream_calls[0]["messages"]

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
async def test_fake_llm_runs_out_of_responses_raises_assertion() -> None:
    """Sanity guard on the test harness — under-scripted calls fail loudly."""

    fake = FakeLLMClient(complete_responses=[])
    orchestrator, _js, _ts = _make_orchestrator(fake)

    with pytest.raises(AssertionError):
        await orchestrator.process_user_message("s1", "x")
