"""``addendum_task`` tool — push info into a running sub-agent without restart.

PRD 0006 / issue 0050. Pre-0050 the only way to add information to a
running sub-task was to cancel + respawn it (slice #0021's ``ask_user``
flow plus the deprecated ``forward_to_subtask`` tool, which only worked
on ``waiting_input`` tasks). The v2 contract:

* The user says "ajoute X" or similar; Jarvis dispatches
  ``addendum_task(task_id, info)``.
* The handler resolves the live runner via
  :attr:`ToolHandlerContext.addendum_queue_factory` and pushes ``info``
  into the per-task :class:`bob.sub_agent.addendum_queue.AddendumQueue`.
* The runner drains the queue at the next iteration boundary (the
  wiring shipped in 0045) and folds the addendum into the next LLM
  prompt — no restart, no state change. Existing sub-agent reasoning
  continues, augmented.

Failure modes:

* Unknown ``task_id`` → ``unknown_task`` error.
* Task not in ``running`` state → ``task_not_running``. The handler is
  intentionally strict: addenda only make sense while the sub-agent is
  actively iterating. ``spawned`` / ``awaiting_input`` tasks must be
  scheduled / resumed first.
* No live queue (orchestrator without the factory wired) →
  ``addendum_unavailable`` so tests that exercise the registry in
  isolation get a clean error rather than a silent drop.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from bob.debug_log import emit_debug
from bob.task_store import TaskStoreError
from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome

_logger = structlog.get_logger(__name__)


class AddendumTaskArgs(BaseModel):
    """Validated argument shape for the ``addendum_task`` tool (v1)."""

    task_id: str = Field(
        ...,
        min_length=1,
        description=(
            "ID exact de la sous-tâche concernée — le bloc STATE en tête de "
            "prompt liste l'``id`` de chaque tâche active."
        ),
    )
    info: str = Field(
        ...,
        min_length=1,
        description=(
            "Information à transmettre au sub-agent. Sera lue à la "
            "prochaine itération du runner sans redémarrer la tâche."
        ),
    )


_ADDENDUM_DESCRIPTION = (
    "Ajoute une information à une sous-tâche en cours sans la redémarrer. "
    "Le sub-agent reçoit la note à sa prochaine boucle et la prend en "
    "compte pour la suite. À utiliser quand l'utilisateur veut enrichir "
    "une tâche déjà lancée (« ajoute X », « précise Y »)."
)


_ADDENDUM_PARAMETERS = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "ID exact de la sous-tâche concernée (depuis le bloc STATE).",
        },
        "info": {
            "type": "string",
            "description": "Information à transmettre, telle quelle.",
        },
    },
    "required": ["task_id", "info"],
}


async def _addendum_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Resolve the running runner's :class:`AddendumQueue` and push ``info``."""

    assert isinstance(args, AddendumTaskArgs)
    target_id = args.task_id.strip()
    info = args.info.strip()
    if not target_id:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="task_id is empty after strip",
        )
    if not info:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="info is empty after strip",
        )

    try:
        task = ctx.task_store.get_task(target_id)
    except TaskStoreError:
        _logger.warning("orchestrator.addendum_unknown_task", task_id=target_id)
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="unknown_task",
            error_message=f"unknown task: {target_id}",
        )

    if task.state != "running":
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="task_not_running",
            error_message=f"task {target_id} is in state {task.state}",
        )

    if ctx.addendum_queue_factory is None:
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="addendum_unavailable",
            error_message="addendum_queue_factory not wired into ToolHandlerContext",
        )

    queue = ctx.addendum_queue_factory(target_id)
    if queue is None:
        # The task is ``running`` per the SQL row but no live runner is
        # tracked by the boot path. This is the same defensive corner
        # the scheduler logs as ``cancel_missing_runner`` — surface a
        # structured error so Jarvis can degrade gracefully.
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="addendum_unavailable",
            error_message=(
                f"no live runner registered for {target_id}; "
                "task row says running but runner pool is empty"
            ),
        )

    queue.put(info)
    emit_debug(
        category="decision",
        severity="info",
        source="orchestrator._dispatch_addendum_task",
        summary=f"Jarvis ajoute info à '{task.title}'",
        payload={
            "task_id": target_id,
            "title": task.title,
            "info_chars": len(info),
        },
    )
    _logger.info(
        "orchestrator.addendum_task",
        task_id=target_id,
        info_chars=len(info),
    )
    return ToolHandlerOutcome(status="ok", task_id=target_id)


def build_addendum_task_tool() -> ToolDefinition:
    """Construct the registry entry for ``addendum_task`` (v1)."""

    return ToolDefinition(
        name="addendum_task",
        version="v1",
        description=_ADDENDUM_DESCRIPTION,
        parameters=_ADDENDUM_PARAMETERS,
        args_model=AddendumTaskArgs,
        handler=_addendum_handler,
    )
