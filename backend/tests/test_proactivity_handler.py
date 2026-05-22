"""Tests for :mod:`bob.proactivity_handler`."""

from __future__ import annotations

from typing import Any

import pytest

from bob.proactivity_handler import ProactivityHandler


class _FakeOrchestrator:
    """Minimal orchestrator double recording proactive-message calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_proactive_message(self, task_id: str, event_kind: str) -> None:
        self.calls.append((task_id, event_kind))


@pytest.mark.asyncio
async def test_ask_user_event_invokes_orchestrator() -> None:
    fake = _FakeOrchestrator()
    handler = ProactivityHandler(orchestrator_factory=lambda: fake)

    await handler.on_task_state_changed(
        {
            "task_id": "abc",
            "old_state": "running",
            "new_state": "waiting_input",
            "action": "ask_user",
        }
    )

    assert fake.calls == [("abc", "ask_user")]


@pytest.mark.asyncio
async def test_done_event_dispatches_to_done_synthesis() -> None:
    """Slice #0025: ``done`` state transitions trigger a ``done`` proactive event."""

    fake = _FakeOrchestrator()
    handler = ProactivityHandler(orchestrator_factory=lambda: fake)

    await handler.on_task_state_changed(
        {
            "task_id": "abc",
            "old_state": "running",
            "new_state": "done",
            "action": "done",
        }
    )

    assert fake.calls == [("abc", "done")]


@pytest.mark.asyncio
async def test_done_event_without_action_still_dispatches() -> None:
    """The ``action`` field on the payload is optional for the done branch.

    The sub-agent runner sets ``action="done"`` when transitioning to done,
    but the handler must remain robust if upstream evolves and the field
    disappears. The state itself is the gating signal.
    """

    fake = _FakeOrchestrator()
    handler = ProactivityHandler(orchestrator_factory=lambda: fake)

    await handler.on_task_state_changed(
        {
            "task_id": "abc",
            "old_state": "running",
            "new_state": "done",
        }
    )

    assert fake.calls == [("abc", "done")]


@pytest.mark.asyncio
async def test_failed_event_is_noop() -> None:
    fake = _FakeOrchestrator()
    handler = ProactivityHandler(orchestrator_factory=lambda: fake)

    await handler.on_task_state_changed(
        {
            "task_id": "abc",
            "old_state": "running",
            "new_state": "failed",
        }
    )

    assert fake.calls == []


@pytest.mark.asyncio
async def test_running_promotion_is_noop() -> None:
    """``pending → running`` transitions must not trigger Jarvis."""

    fake = _FakeOrchestrator()
    handler = ProactivityHandler(orchestrator_factory=lambda: fake)

    await handler.on_task_state_changed(
        {
            "task_id": "abc",
            "old_state": "pending",
            "new_state": "running",
        }
    )

    assert fake.calls == []


@pytest.mark.asyncio
async def test_bad_payload_is_silently_ignored() -> None:
    fake = _FakeOrchestrator()
    handler = ProactivityHandler(orchestrator_factory=lambda: fake)

    # No ``task_id`` → log + return, no crash.
    bad_payload: dict[str, Any] = {"new_state": "waiting_input", "action": "ask_user"}
    await handler.on_task_state_changed(bad_payload)

    assert fake.calls == []
