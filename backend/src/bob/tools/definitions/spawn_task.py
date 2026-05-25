"""``spawn_task`` tool definition — v2 replacement for ``spawn_subtask``.

PRD 0006 / issue 0050. ``spawn_task`` keeps the same wire shape as the
v1 ``spawn_subtask`` (a title + a goal) but:

* It is the canonical v2 entry point: the bounded prompt advertises it
  alongside ``addendum_task`` / ``replan_task`` / ``cancel_task`` so
  Jarvis composes operations around a single task lifecycle.
* It is wired through :class:`bob.scheduler_policy.SchedulerPolicy`,
  surfacing :class:`bob.scheduler_policy.SchedulerQueueFull` as a
  structured ``scheduler_queue_full`` error so Jarvis can degrade with
  a clarifying speech (PRD user story #33).
* The persisted row's state lands in ``spawned`` (the v2 lifecycle
  start node), distinguishing v2 tasks from any legacy ``pending``
  rows.

The v1 ``spawn_subtask`` tool stays in the registry as a deprecated
alias so existing tests + integration paths continue to work; both
tools are dispatched through the same ToolDispatcher and emit
``jarvis.route`` events for audit. A future cleanup slice removes the
v1 entry once every call site has migrated.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from bob.debug_log import emit_debug
from bob.scheduler_policy import SCHEDULER_QUEUE_FULL_ERROR_CODE, SchedulerQueueFull
from bob.task_store import TaskStoreError
from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome

_logger = structlog.get_logger(__name__)


class SpawnTaskArgs(BaseModel):
    """Validated argument shape for the ``spawn_task`` tool (v1)."""

    title: str = Field(
        ...,
        min_length=1,
        description="Titre court (≤ 8 mots) pour la sidebar et le bloc STATE.",
    )
    goal: str = Field(
        ...,
        min_length=1,
        description="Goal précis et complet pour le sub-agent.",
    )


_SPAWN_TASK_DESCRIPTION = (
    "Délègue une tâche longue ou autonome à un sub-agent en arrière-plan "
    "(version v2 du surface PRD 0006). La tâche apparaît dans le bloc STATE "
    "avec son ``id`` exact pour les outils ``addendum_task`` / "
    "``replan_task`` / ``cancel_task``. Utilise ceci quand l'utilisateur "
    "demande quelque chose qui prend du temps ; pour les questions simples, "
    "appelle ``say``."
)


_SPAWN_TASK_PARAMETERS = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Titre court (≤ 8 mots) pour la sidebar et le bloc STATE.",
        },
        "goal": {
            "type": "string",
            "description": "Goal précis et complet pour le sub-agent.",
        },
    },
    "required": ["title", "goal"],
}


async def _spawn_task_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Create the v2 task row, emit ``task_created``, enqueue with the cap."""

    assert isinstance(args, SpawnTaskArgs)
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
    # Move the freshly-created task from the legacy ``pending`` default
    # to the v2 ``spawned`` start node so the scheduler counts it under
    # the v2 queue cap. Both states are valid in the union; the v2
    # tools own ``spawned`` while the legacy tool keeps ``pending``.
    try:
        ctx.task_store.update_state(task_id, "spawned")
    except TaskStoreError:
        # Defensive: if the migration has not added the state literal
        # yet we leave the row in ``pending`` and let the queue cap
        # still count it.
        _logger.warning(
            "spawn_task.spawned_transition_failed",
            task_id=task_id,
        )

    created = ctx.task_store.get_task(task_id)
    emit_debug(
        category="decision",
        severity="info",
        source="orchestrator._dispatch_spawn_task",
        summary=f"Jarvis lance task v2 '{title}'",
        payload={
            "task_id": task_id,
            "title": created.title,
            "goal": created.goal,
            "state": created.state,
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

    try:
        await ctx.task_scheduler.enqueue(task_id)
    except SchedulerQueueFull as exc:
        # Roll the row back to ``failed`` with a structured reason so
        # the audit trail records the overflow without leaving a
        # stalled ``spawned`` row sitting in the queue forever.
        try:
            ctx.task_store.update_state(task_id, "failed")
            ctx.task_store.set_result(task_id, "scheduler_queue_full")
        except TaskStoreError:
            _logger.exception("spawn_task.rollback_failed", task_id=task_id)
        _logger.warning(
            "orchestrator.spawn_task_queue_full",
            task_id=task_id,
            running=exc.running,
            queued=exc.queued,
            max_running=exc.max_running,
            max_queued=exc.max_queued,
        )
        return ToolHandlerOutcome(
            status="error",
            task_id=task_id,
            error_code=SCHEDULER_QUEUE_FULL_ERROR_CODE,
            error_message=str(exc),
        )

    _logger.info("orchestrator.spawned_task", task_id=task_id, title=title)
    return ToolHandlerOutcome(status="ok", task_id=task_id)


def build_spawn_task_tool() -> ToolDefinition:
    """Construct the registry entry for ``spawn_task`` (v1)."""

    return ToolDefinition(
        name="spawn_task",
        version="v1",
        description=_SPAWN_TASK_DESCRIPTION,
        parameters=_SPAWN_TASK_PARAMETERS,
        args_model=SpawnTaskArgs,
        handler=_spawn_task_handler,
    )
