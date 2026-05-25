"""``cancel_task`` tool — v2 replacement for ``cancel_subtask``.

PRD 0006 / issue 0050. Behaviour mirrors v1 ``cancel_subtask`` exactly:
the scheduler is permissive on unknown / terminal ids, the reason
defaults to ``user_cancelled`` when omitted, and the dispatcher emits
a ``jarvis.route`` event around the call. The v2 entry exists so the
prompt addendum can advertise a coherent v2 surface
(``spawn_task`` / ``addendum_task`` / ``replan_task`` / ``cancel_task``)
rather than mixing v1 and v2 tool names.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome

_logger = structlog.get_logger(__name__)


class CancelTaskArgs(BaseModel):
    """Validated argument shape for ``cancel_task`` (v1)."""

    task_id: str = Field(
        ...,
        min_length=1,
        description=(
            "ID exact de la sous-tâche à annuler — le bloc STATE en tête de "
            "prompt liste l'``id`` de chaque tâche active."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="Raison brève. Default 'user_cancelled'.",
    )


_CANCEL_DESCRIPTION = (
    "Annule une sous-tâche en cours. À appeler quand l'utilisateur "
    "demande explicitement d'arrêter une tâche listée dans le bloc STATE."
)


_CANCEL_PARAMETERS = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "ID exact de la sous-tâche à annuler (depuis le bloc STATE).",
        },
        "reason": {
            "type": "string",
            "description": "Raison brève. Default 'user_cancelled'.",
        },
    },
    "required": ["task_id"],
}


async def _cancel_task_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Route a ``cancel_task`` call to the scheduler."""

    assert isinstance(args, CancelTaskArgs)
    target_id = args.task_id.strip()
    if not target_id:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="task_id is empty after strip",
        )

    raw_reason = args.reason
    reason = (
        raw_reason.strip()
        if isinstance(raw_reason, str) and raw_reason.strip()
        else "user_cancelled"
    )

    await ctx.task_scheduler.cancel(target_id, reason=reason)
    _logger.info(
        "orchestrator.cancel_task",
        task_id=target_id,
        reason=reason,
    )
    return ToolHandlerOutcome(status="ok", task_id=target_id)


def build_cancel_task_tool() -> ToolDefinition:
    """Construct the registry entry for ``cancel_task`` (v1)."""

    return ToolDefinition(
        name="cancel_task",
        version="v1",
        description=_CANCEL_DESCRIPTION,
        parameters=_CANCEL_PARAMETERS,
        args_model=CancelTaskArgs,
        handler=_cancel_task_handler,
    )
