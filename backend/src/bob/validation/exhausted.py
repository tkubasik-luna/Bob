"""``on_validation_exhausted`` interface + default handlers (PRD 0006 / issue 0048).

A single, narrow Protocol the orchestrator and the sub-agent runner
both implement so the degrade contract is shared. When a retry budget
is exhausted the dispatcher calls
:meth:`OnValidationExhausted.on_validation_exhausted` on the registered
handler and walks away — concrete degrade behaviour lives entirely on
the handler side.

The Jarvis-side default handler emits a hardcoded ``say`` call through
the existing tool dispatcher (never bypassing it) and logs a structured
``jarvis.validation_failed`` event. The sub-agent-side default handler
forces a ``done(status=failed, reason_code=invalid_output)`` row with
``lineage`` preserved from the parent task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from bob.context.prompt_fragments import PromptFragment
from bob.validation.envelope import CallEnvelope
from bob.validation.reason_codes import REASON_INVALID_OUTPUT, REASON_VALIDATION_EXHAUSTED

_logger = structlog.get_logger(__name__)


#: Hardcoded French phrase Jarvis emits when validation cannot recover.
#: Lives as a :class:`PromptFragment` so the wording is versioned and the
#: snapshot tests can pin to ``version=1``. Bumping the wording is a
#: deliberate, reviewable lever.
JARVIS_DEGRADE_SPEECH_FRAGMENT = PromptFragment(
    id="jarvis_validation_exhausted_speech",
    version=1,
    template="Désolé, peux-tu reformuler ?",
    description=(
        "Hardcoded say() speech emitted by Jarvis when the validation "
        "retry budget is exhausted (PRD 0006 / issue 0048). Routed "
        "through the SayTool via the dispatcher — never bypasses it."
    ),
)


#: Identifier used in the system_validator role label fragment so call
#: sites can audit which versioned phrasing was injected. The actual role
#: literal lives in :mod:`bob.validation.system_validator`; the fragment
#: just gives us a versioned anchor for golden prompt tests.
SYSTEM_VALIDATOR_ROLE_FRAGMENT = PromptFragment(
    id="system_validator_role_label",
    version=1,
    template="system_validator",
    description=(
        "Versioned label for the dedicated validator role injected by "
        "the retry path (PRD 0006 / issue 0048). The fragment exists so "
        "snapshot tests can lock in the role string in addition to the "
        "wording fragments."
    ),
)


@dataclass(frozen=True)
class ExhaustedContext:
    """Bag handed to the ``on_validation_exhausted`` handler.

    Carries everything the default handlers need to record the failure
    + emit the appropriate degrade signal:

    - ``envelope`` — the transient call envelope that exhausted its budget.
    - ``last_error_message`` — human-readable reason the validator
      stopped retrying.
    - ``task_id`` — the sub-agent task id, when the actor is the runner.
      ``None`` for the Jarvis path (the orchestrator turn doesn't have a
      task id of its own).
    """

    envelope: CallEnvelope
    last_error_message: str
    task_id: str | None = None


class OnValidationExhausted(Protocol):
    """Async handler invoked when the retry budget is spent."""

    async def on_validation_exhausted(self, context: ExhaustedContext) -> None: ...


class JarvisOnValidationExhausted:
    """Default Jarvis-side handler.

    Dispatches a hardcoded ``say(speech="Désolé, peux-tu reformuler ?")``
    through the live :class:`bob.tools.ToolDispatcher` so the degrade
    path goes through the **same** code that handles every other tool
    call (route event, persistence in :class:`JarvisStore`, WS emission
    on the orchestrator side). Logging the structured
    ``jarvis.validation_failed`` event is the second side effect — and
    both happen even when the dispatcher itself errors (defensive net).
    """

    def __init__(self, *, dispatcher: Any) -> None:
        self._dispatcher = dispatcher
        self._last_speech: str | None = None

    @property
    def last_speech(self) -> str | None:
        """Last spoken degrade text — exposed for orchestrator wiring."""

        return self._last_speech

    async def on_validation_exhausted(self, context: ExhaustedContext) -> None:
        speech = JARVIS_DEGRADE_SPEECH_FRAGMENT.template
        self._last_speech = speech

        # Build a synthetic ``say`` ToolCall and route it through the live
        # dispatcher so the SayTool's persistence + route-event side
        # effects fire exactly like a normal turn. Imported lazily so
        # the validation package does not eagerly depend on ``bob.llm``.
        from bob.llm.types import ToolCall

        call = ToolCall(
            id="call_validation_exhausted",
            name="say",
            arguments={"speech": speech, "ui": None},
        )
        try:
            await self._dispatcher.dispatch(call)
        except Exception:  # pragma: no cover — defensive net.
            _logger.exception("jarvis.validation_failed_dispatch_failed")

        _logger.warning(
            "jarvis.validation_failed",
            tool=context.envelope.tool_name,
            attempts=context.envelope.attempts,
            retries_used=context.envelope.retries_used,
            error_code=context.envelope.last_error_code or REASON_VALIDATION_EXHAUSTED,
            last_error_message=context.last_error_message,
        )


class SubAgentOnValidationExhausted:
    """Default sub-agent-side handler.

    Calls back into the runner's existing ``_finalize_done`` path so the
    forced ``done(status=failed, reason_code=invalid_output)`` keeps
    every side effect (lineage preservation, task_state_changed bus
    event, task_message WS frame) the runner already wires.
    """

    def __init__(self, *, runner: Any) -> None:
        self._runner = runner

    async def on_validation_exhausted(self, context: ExhaustedContext) -> None:
        assert context.task_id is not None, "sub-agent context must carry task_id"
        await self._runner.force_failed_invalid_output(
            task_id=context.task_id,
            error_message=context.last_error_message,
        )


__all__ = [
    "JARVIS_DEGRADE_SPEECH_FRAGMENT",
    "REASON_INVALID_OUTPUT",
    "REASON_VALIDATION_EXHAUSTED",
    "SYSTEM_VALIDATOR_ROLE_FRAGMENT",
    "ExhaustedContext",
    "JarvisOnValidationExhausted",
    "OnValidationExhausted",
    "SubAgentOnValidationExhausted",
]
