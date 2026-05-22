"""Bridge between the EventBus and Jarvis' proactive chat pushes (slice #0021).

When a sub-agent runner publishes ``task_state_changed`` with
``new_state="waiting_input"`` and ``action="ask_user"``, the
:class:`ProactivityHandler` calls
:meth:`Orchestrator.generate_proactive_message` so Jarvis paraphrases the
raw sub-agent question and pushes it back to the user via the WS layer.

Done synthesis (``new_state="done"``) is reserved for slice #0025 — the
handler only logs and returns for now so the future wiring is a one-line
addition.

The handler is intentionally tiny: it knows how to read a
``task_state_changed`` payload and dispatch to the right orchestrator
method. Anything else (paraphrase prompt, WS emission) lives in the
orchestrator.

Note (slice #0022): the handler is subscribed exclusively to
``task_state_changed``. ``progress`` events publish on
``task_message_added`` only (no state transition) so this handler does not
fire on progress — Jarvis stays silent in the main chat while progress
statuses flow to the sidebar.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import structlog

_logger = structlog.get_logger(__name__)


class _OrchestratorProtocol(Protocol):
    async def generate_proactive_message(self, task_id: str, event_kind: str) -> None: ...


OrchestratorFactory = Callable[[], _OrchestratorProtocol]


class ProactivityHandler:
    """EventBus subscriber that turns sub-agent transitions into Jarvis messages."""

    def __init__(self, *, orchestrator_factory: OrchestratorFactory) -> None:
        self._orchestrator_factory = orchestrator_factory

    async def on_task_state_changed(self, payload: dict[str, Any]) -> None:
        """Subscribe target for the ``task_state_changed`` topic.

        Handles only ``ask_user`` transitions in this slice; ``done``
        synthesis is a no-op (slice #0025).
        """

        new_state = payload.get("new_state")
        action = payload.get("action")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            _logger.warning("proactivity.bad_payload", payload=payload)
            return

        if new_state == "waiting_input" and action == "ask_user":
            orchestrator = self._orchestrator_factory()
            await orchestrator.generate_proactive_message(task_id=task_id, event_kind="ask_user")
            return

        # `done` synthesis lands in slice #0025. We log so we can still see
        # the event flow in dev. Failed transitions are explicitly ignored —
        # the WS task_updated already surfaced the state change.
        _logger.debug(
            "proactivity.no_op",
            task_id=task_id,
            new_state=new_state,
            action=action,
        )
