"""``spawn_subtask`` tool definition.

Behavior is identical to the pre-0044 :func:`Orchestrator._dispatch_spawn`:
create a task row in the :class:`bob.task_store.TaskStore`, emit the
``task_created`` WebSocket event, and hand the new id to the scheduler
via :meth:`bob.task_scheduler.TaskScheduler.enqueue`. The migration to
the dispatcher does not change any of these side effects (PRD 0006,
issue 0044: "behavior unchanged").

Pydantic v2 :class:`SpawnSubtaskArgs` validates the LLM arguments. The
JSON schema served to the LLM is kept in lock-step with the model (the
contract test ``tests/test_tool_spawn.py`` asserts the required-field
set matches).
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from bob.debug_log import emit_debug
from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome

_logger = structlog.get_logger(__name__)


# ``min_length=1`` after ``str.strip()`` reproduces the legacy guard that
# rejected whitespace-only titles / goals. We use ``min_length`` rather
# than a custom validator so the JSON schema we expose to the LLM stays
# decla­rative.
class SpawnSubtaskArgs(BaseModel):
    """Validated argument shape for the ``spawn_subtask`` tool."""

    title: str = Field(..., min_length=1, description="Titre court (1-5 mots) pour la sidebar.")
    goal: str = Field(..., min_length=1, description="Goal précis et complet pour le sub-agent.")


# Description and JSON-Schema parameters mirror the pre-0044 inline
# :class:`ToolDefinition` so the LLM prompt + tool list is byte-equal.
_SPAWN_DESCRIPTION = (
    "Délègue une tâche longue ou autonome à un sub-agent en arrière-plan. "
    "Utilise ceci quand l'utilisateur demande quelque chose qui prend du "
    "temps (recherche, draft d'email, analyse) ou qui peut tourner sans "
    "ton intervention. Pour les questions simples, réponds directement "
    "en texte sans appeler cet outil."
)


_SPAWN_PARAMETERS = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Titre court (1-5 mots) pour la sidebar.",
        },
        "goal": {
            "type": "string",
            "description": "Goal précis et complet pour le sub-agent.",
        },
    },
    "required": ["title", "goal"],
}


async def _spawn_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Create the task, emit ``task_created``, enqueue with the scheduler.

    Returns ``ToolHandlerOutcome(status="ok", task_id=<new id>)``. The
    dispatcher emits the canonical ``jarvis.route`` event around the
    handler; this handler keeps the orchestrator-style ``emit_debug``
    "Jarvis lance sub-task …" line so the debug-view turn grouping does
    not regress.
    """

    assert isinstance(args, SpawnSubtaskArgs)
    title = args.title.strip()
    goal = args.goal.strip()
    if not title:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="title is empty after strip",
        )
    if not goal:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="goal is empty after strip",
        )

    task_id = ctx.task_store.create_task(title=title, goal=goal)
    created = ctx.task_store.get_task(task_id)
    emit_debug(
        category="decision",
        severity="info",
        source="orchestrator._dispatch_spawn",
        summary=f"Jarvis lance sub-task '{title}'",
        payload={
            "task_id": task_id,
            "title": created.title,
            "goal": created.goal,
        },
    )
    await ctx.ws_emit(
        {
            "type": "task_created",
            "task_id": task_id,
            "title": created.title,
            "goal": created.goal,
            "state": created.state,
            "created_at": created.created_at,
        }
    )
    await ctx.task_scheduler.enqueue(task_id)
    _logger.info("orchestrator.spawned_subtask", task_id=task_id, title=title)
    return ToolHandlerOutcome(status="ok", task_id=task_id)


def build_spawn_subtask_tool() -> ToolDefinition:
    """Construct the registry entry for ``spawn_subtask`` (v1)."""

    return ToolDefinition(
        name="spawn_subtask",
        version="v1",
        description=_SPAWN_DESCRIPTION,
        parameters=_SPAWN_PARAMETERS,
        args_model=SpawnSubtaskArgs,
        handler=_spawn_handler,
    )
