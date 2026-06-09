"""Tests for :mod:`bob.task_supervisor` (PRD 0018 / issue 0124).

External behavior only: a supervised task that raises produces a ``system``
debug event (read back from the ring buffer, the same sink ``/ws/debug``
serves) carrying the spawn-site context, plus a structured ERROR log line.
Success and cancellation report nothing. The adopted sites (event-bus
subscriber dispatch, the orchestrator's proactive flusher) are exercised the
same way — assertions on emitted events, never on internal call sequences.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, cast

import pytest

from bob import debug_log
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.event_bus import EventBus
from bob.jarvis_store import JarvisStore
from bob.llm_client import LLMClient
from bob.orchestrator import Orchestrator
from bob.task_store import TaskStore
from bob.task_supervisor import create_supervised_task, supervise


@pytest.fixture(autouse=True)
def _clear_buffer() -> None:
    debug_log.clear()


def _supervisor_events() -> list[debug_log.DebugEvent]:
    return [e for e in debug_log.snapshot() if e.source == "bob.task_supervisor"]


async def _drain() -> None:
    """Yield enough event-loop ticks for done-callbacks to run."""

    for _ in range(5):
        await asyncio.sleep(0)


# --- supervise / create_supervised_task --------------------------------------


async def test_supervised_failure_emits_debug_event_with_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    task = create_supervised_task(
        _boom(),
        name="tts.proactive_synthesis",
        session_id="s1",
        turn_id="t1",
        msg_id="m1",
        context={"extra": "field"},
    )
    with pytest.raises(RuntimeError):
        await task
    await _drain()

    [event] = _supervisor_events()
    assert event.category == "system"
    assert event.severity == "error"
    assert "tts.proactive_synthesis" in event.summary
    assert "RuntimeError" in event.summary
    assert event.payload["task_name"] == "tts.proactive_synthesis"
    assert event.payload["error"] == "RuntimeError: kaboom"
    assert event.payload["session_id"] == "s1"
    assert event.payload["turn_id"] == "t1"
    assert event.payload["msg_id"] == "m1"
    assert event.payload["extra"] == "field"
    assert event.turn_id == "t1"

    # The structured ERROR log landed too (structlog prints to stdout).
    captured = capsys.readouterr()
    assert "task_supervisor.task_failed" in captured.out + captured.err


async def test_supervised_success_and_cancel_emit_nothing() -> None:
    async def _ok() -> str:
        return "fine"

    async def _sleepy() -> None:
        await asyncio.sleep(60)

    ok_task = create_supervised_task(_ok(), name="noop")
    assert await ok_task == "fine"

    sleepy = create_supervised_task(_sleepy(), name="sleepy")
    sleepy.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sleepy
    await _drain()

    assert _supervisor_events() == []


async def test_supervise_returns_the_same_task() -> None:
    async def _ok() -> None:
        return None

    inner = asyncio.create_task(_ok())
    assert supervise(inner, name="identity") is inner
    await inner
    await _drain()
    assert _supervisor_events() == []


# --- adoption: event-bus subscriber dispatch ----------------------------------


async def test_event_bus_failing_subscriber_emits_event_with_topic() -> None:
    """A crashing handler is reported with its topic; siblings still receive."""

    bus = EventBus()
    received: list[dict[str, Any]] = []

    async def boom(_payload: dict[str, Any]) -> None:
        raise ValueError("handler broken")

    async def good(payload: dict[str, Any]) -> None:
        received.append(payload)

    bus.subscribe("task_state_changed", boom)
    bus.subscribe("task_state_changed", good)
    await bus.publish("task_state_changed", {"task_id": "x"})
    await _drain()

    # The sibling subscriber still got the payload (bus not poisoned).
    assert received == [{"task_id": "x"}]

    [event] = _supervisor_events()
    assert event.payload["task_name"] == "event_bus.subscriber"
    assert event.payload["topic"] == "task_state_changed"
    assert "boom" in event.payload["subscriber"]
    assert event.payload["error"] == "ValueError: handler broken"


# --- adoption: the orchestrator's proactive flusher ----------------------------


def _make_orchestrator() -> Orchestrator:
    """Minimal orchestrator — the LLM client / scheduler are never reached."""

    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return Orchestrator(
        jarvis_client=cast(LLMClient, object()),
        jarvis_store=JarvisStore(conn),
        task_store=TaskStore(conn),
        task_scheduler=cast(Any, object()),
        jarvis_prompt="Tu es Jarvis-de-test.",
    )


async def test_proactive_flusher_crash_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead flusher produces a log + debug event — never a silent stop.

    Fault injection: the flusher loop has no public failure seam (every
    per-item exception is contained by design), so we substitute the loop
    body with one that dies immediately. The assertion stays external — the
    supervisor's debug event is what an operator would see on ``/ws/debug``.
    """

    orchestrator = _make_orchestrator()

    async def _dead_flusher() -> None:
        raise RuntimeError("flusher dead")

    monkeypatch.setattr(orchestrator, "_flush_proactive_loop", _dead_flusher)
    orchestrator.start_proactive_loop()
    await _drain()

    [event] = _supervisor_events()
    assert event.payload["task_name"] == "orchestrator.proactive_flusher"
    assert event.payload["error"] == "RuntimeError: flusher dead"

    await orchestrator.stop_proactive_loop()


async def test_proactive_flusher_clean_cancel_emits_nothing() -> None:
    """The normal lifespan stop (cancel) is a clean outcome, not a failure."""

    orchestrator = _make_orchestrator()
    orchestrator.start_proactive_loop()
    await _drain()
    await orchestrator.stop_proactive_loop()
    await _drain()

    assert _supervisor_events() == []
