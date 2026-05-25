"""Tests for the sub-agent validation retry path (PRD 0006 / issue 0048).

Verifies the runner uses :class:`bob.validation.CallEnvelope` to retry
malformed LLM payloads before falling through to the forced
``done(failed, invalid_output)`` end state, and that the retry call
carries a ``system_validator`` feedback message.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent import REASON_INVALID_OUTPUT, SubAgentPolicy, SubAgentRunner
from bob.task_store import TaskStore
from bob.validation.system_validator import SYSTEM_VALIDATOR_ROLE


class _ScriptedClient(LLMClient):
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        self.calls.append({"messages": list(messages)})
        if not self._values:
            raise AssertionError("scripted client out of values")
        return self._values.pop(0)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError


def _make_store() -> TaskStore:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return TaskStore(conn)


def _running_task(store: TaskStore) -> str:
    task_id = store.create_task(title="t", goal="do it")
    store.update_state(task_id, "running")
    return task_id


def _done_payload(result: str = "ok") -> str:
    return json.dumps(
        {
            "action": "done",
            "result_summary": result,
            "ui_payload": None,
            "status": "complete",
            "reason_code": "ok",
            "cost": {},
        }
    )


@pytest.mark.asyncio
async def test_malformed_payload_then_valid_recovers() -> None:
    """A first invalid JSON output gets retried with system_validator feedback."""

    store = _make_store()
    task_id = _running_task(store)
    client = _ScriptedClient(["{ not json", _done_payload("retried-ok")])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=999_999),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "done"
    assert task.result == "retried-ok"

    # The retry call (calls[1]) carried a ``system_validator`` message.
    retry_messages = client.calls[1]["messages"]
    validator_msgs = [m for m in retry_messages if m["role"] == SYSTEM_VALIDATOR_ROLE]
    assert len(validator_msgs) == 1
    assert "invalide" in validator_msgs[0]["content"]


@pytest.mark.asyncio
async def test_two_malformed_payloads_force_failed_invalid_output() -> None:
    """Exhausted retry budget → forced done(failed, invalid_output)."""

    store = _make_store()
    task_id = _running_task(store)
    client = _ScriptedClient(["not json at all", "still not json"])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=999_999),
    )

    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    # Reason recorded as system message body (matches runner v2 behaviour).
    system_rows = [m for m in store.get_task_messages(task_id) if m.role == "system"]
    assert system_rows
    assert REASON_INVALID_OUTPUT in system_rows[-1].content or "invalid" in system_rows[-1].content


@pytest.mark.asyncio
async def test_force_failed_invalid_output_preserves_lineage() -> None:
    """The forced terminal done() preserves the existing task row.

    The runner's ``force_failed_invalid_output`` goes through
    ``_finalize_done`` so the row state flip + system message
    persistence matches the regular fail path. Lineage is preserved on
    the task row throughout (never cleared by the validation degrade).
    """

    store = _make_store()
    parent_id = store.create_task(title="parent", goal="p")
    task_id = store.create_task(title="child", goal="c", lineage=[parent_id])
    store.update_state(task_id, "running")

    client = _ScriptedClient(["junk", "junk2"])
    runner = SubAgentRunner(
        subagent_client=client,
        task_store=store,
        event_bus=EventBus(),
        policy=SubAgentPolicy(max_iterations=99, wall_clock_seconds=999.0, token_cap=999_999),
    )
    await runner.run(task_id)

    task = store.get_task(task_id)
    assert task.state == "failed"
    # Lineage is unchanged.
    assert task.lineage == [parent_id]
