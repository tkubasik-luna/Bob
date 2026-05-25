"""``forward_to_subtask`` tool definition.

Mirrors the pre-0044 :func:`Orchestrator._dispatch_forward`: validate the
target id + response, append a ``user`` message to the task's log, emit
the ``task_message`` WS event and ask the scheduler to resume the
runner. Unknown task ids and tasks not in ``waiting_input`` produce a
structured handler error so the dispatcher's ``jarvis.route`` event
records the precise reason code (PRD 0006 user story #19 / issue 0044).
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from bob.task_store import TaskStoreError
from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome

_logger = structlog.get_logger(__name__)


class ForwardToSubtaskArgs(BaseModel):
    """Validated argument shape for ``forward_to_subtask``."""

    task_id: str = Field(
        ...,
        min_length=1,
        description=(
            "ID de la sous-tâche concernée. Le résumé des tâches actives "
            "en tête de prompt liste l'``id`` exact de chaque tâche qui "
            "attend une réponse."
        ),
    )
    response: str = Field(
        ...,
        min_length=1,
        description="La réponse de l'utilisateur à transmettre, telle quelle.",
    )


_FORWARD_DESCRIPTION = (
    "Transmet la réponse de l'utilisateur à une sous-tâche en attente "
    "d'input. À appeler uniquement quand l'utilisateur répond à une "
    "question préalablement transmise par toi pour le compte d'une "
    "tâche en cours."
)


_FORWARD_PARAMETERS = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": (
                "ID de la sous-tâche concernée. Le résumé des tâches "
                "actives en tête de prompt liste l'``id`` exact de chaque "
                "tâche qui attend une réponse."
            ),
        },
        "response": {
            "type": "string",
            "description": "La réponse de l'utilisateur à transmettre, telle quelle.",
        },
    },
    "required": ["task_id", "response"],
}


async def _forward_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Append the user's reply to the target task and resume the runner."""

    assert isinstance(args, ForwardToSubtaskArgs)
    target_id = args.task_id.strip()
    response_text = args.response.strip()
    if not target_id:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="task_id is empty after strip",
        )
    if not response_text:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="response is empty after strip",
        )

    try:
        task = ctx.task_store.get_task(target_id)
    except TaskStoreError:
        _logger.warning("orchestrator.forward_unknown_task", task_id=target_id)
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="unknown_task",
            error_message=f"unknown task: {target_id}",
        )

    if task.state != "waiting_input":
        _logger.warning(
            "orchestrator.forward_wrong_state",
            task_id=target_id,
            state=task.state,
        )
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="task_not_waiting_input",
            error_message=f"task {target_id} is in state {task.state}",
        )

    try:
        message_id = ctx.task_store.append_message(target_id, role="user", content=response_text)
    except TaskStoreError:
        _logger.exception("orchestrator.forward_append_failed", task_id=target_id)
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="append_failed",
            error_message=f"append_message failed for task {target_id}",
        )

    # Surface the forwarded user reply on any open drawer for this task
    # so the transcript reflects the live multi-turn flow.
    try:
        for msg in ctx.task_store.get_task_messages(target_id):
            if msg.id != message_id:
                continue
            await ctx.ws_emit(
                {
                    "type": "task_message",
                    "task_id": target_id,
                    "message_id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "action": msg.action,
                    "created_at": msg.created_at,
                }
            )
            break
    except TaskStoreError:
        _logger.exception("orchestrator.forward_emit_message_failed", task_id=target_id)

    await ctx.task_scheduler.resume(target_id)
    _logger.info("orchestrator.forwarded_to_subtask", task_id=target_id)
    return ToolHandlerOutcome(status="ok", task_id=target_id)


def build_forward_to_subtask_tool() -> ToolDefinition:
    """Construct the registry entry for ``forward_to_subtask`` (v1)."""

    return ToolDefinition(
        name="forward_to_subtask",
        version="v1",
        description=_FORWARD_DESCRIPTION,
        parameters=_FORWARD_PARAMETERS,
        args_model=ForwardToSubtaskArgs,
        handler=_forward_handler,
    )
