"""``cancel_subtask`` tool definition.

Mirrors the pre-0044 :func:`Orchestrator._dispatch_cancel`: validate the
``task_id`` (the scheduler is permissive on unknown / terminal ids — a
cancel against an already-done task is a no-op), default the reason to
``"user_cancelled"`` when omitted, then call
:meth:`bob.task_scheduler.TaskScheduler.cancel`. The handler returns the
target id on ``ok`` so the dispatcher's :class:`DispatchResult` carries
it for the orchestrator's response shape.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome

_logger = structlog.get_logger(__name__)


class CancelSubtaskArgs(BaseModel):
    """Validated argument shape for ``cancel_subtask``.

    ``reason`` is optional. When the LLM omits it we default to
    ``"user_cancelled"`` inside the handler (matching the legacy code
    path and the WS sidebar contract).
    """

    task_id: str = Field(
        ...,
        min_length=1,
        description=(
            "ID de la sous-tâche à annuler. Le résumé des tâches actives "
            "en tête de prompt liste l'``id`` exact."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="Raison brève. Default 'user_cancelled'.",
    )


_CANCEL_DESCRIPTION = (
    "Annule une sous-tâche en cours. À appeler quand l'utilisateur "
    'demande explicitement d\'arrêter une tâche ("annule X", "laisse '
    'tomber"). Tu peux fournir une raison concise (sinon "user_cancelled" '
    "est utilisé)."
)


_CANCEL_PARAMETERS = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": (
                "ID de la sous-tâche à annuler. Le résumé des tâches "
                "actives en tête de prompt liste l'``id`` exact."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Raison brève. Default 'user_cancelled'.",
        },
    },
    "required": ["task_id"],
}


async def _cancel_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Route a ``cancel_subtask`` call to the scheduler."""

    assert isinstance(args, CancelSubtaskArgs)
    target_id = args.task_id.strip()
    if not target_id:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="task_id is empty after strip",
        )

    # Reproduce the legacy fallback verbatim: any non-string / empty /
    # whitespace-only reason collapses to ``"user_cancelled"``. We keep
    # ``Optional[str]`` on the Pydantic model rather than ``Literal``
    # because Jarvis is free to phrase a reason like "trop long".
    raw_reason = args.reason
    reason = (
        raw_reason.strip()
        if isinstance(raw_reason, str) and raw_reason.strip()
        else "user_cancelled"
    )

    await ctx.task_scheduler.cancel(target_id, reason=reason)
    _logger.info(
        "orchestrator.cancelled_subtask",
        task_id=target_id,
        reason=reason,
    )
    return ToolHandlerOutcome(status="ok", task_id=target_id)


def build_cancel_subtask_tool() -> ToolDefinition:
    """Construct the registry entry for ``cancel_subtask`` (v1)."""

    return ToolDefinition(
        name="cancel_subtask",
        version="v1",
        description=_CANCEL_DESCRIPTION,
        parameters=_CANCEL_PARAMETERS,
        args_model=CancelSubtaskArgs,
        handler=_cancel_handler,
    )
