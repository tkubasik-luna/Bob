"""Bridge between the EventBus and Jarvis' proactive chat pushes (slices #0021/#0025).

When a sub-agent runner publishes ``task_state_changed`` with
``new_state="waiting_input"`` and ``action="ask_user"``, the
:class:`ProactivityHandler` calls
:meth:`Orchestrator.generate_proactive_message` so Jarvis paraphrases the
raw sub-agent question and pushes it back to the user via the WS layer.

Slice #0025 adds the ``done`` branch: on ``new_state="done"`` the handler
dispatches to :meth:`Orchestrator.generate_proactive_message` with
``event_kind="done"`` so Jarvis synthesises the result for the user. Both
branches enqueue on the orchestrator's per-instance proactive queue — the
flusher gates emission on user idleness (typing flag + thinking state).

The handler is intentionally tiny: it knows how to read a
``task_state_changed`` payload and dispatch to the right orchestrator
method. Anything else (paraphrase prompt, WS emission, race-condition
buffering) lives in the orchestrator.

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

        Routes ``waiting_input`` + ``action=ask_user`` to a paraphrased
        question and ``done`` transitions to a result synthesis. Other
        transitions (``running``, ``failed``, …) are no-ops — the
        ``task_updated`` WS event already surfaced them in the sidebar.
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

        if new_state == "done":
            orchestrator = self._orchestrator_factory()
            await orchestrator.generate_proactive_message(task_id=task_id, event_kind="done")
            return

        # ``failed`` and intermediate transitions are explicitly ignored —
        # the WS task_updated already surfaced the state change to the
        # sidebar; Jarvis would have nothing to add in chat.
        _logger.debug(
            "proactivity.no_op",
            task_id=task_id,
            new_state=new_state,
            action=action,
        )
