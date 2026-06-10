"""``replan_task`` tool — cancel + respawn with lineage carry-over.

PRD 0006 / issue 0050. When the user reformulates a sub-task ("non
plutôt fais Y"), the v2 contract favours an explicit replan over a
fuzzy "did the user mean enrichment or replacement?" guess:

1. Cancel the previous task via :meth:`TaskScheduler.cancel`. The
   scheduler honours the cooperative-cancel grace then hard-kill
   fallback wired in 0045.
2. Mark the old row ``superseded`` (v2 terminal state). This
   distinguishes "user changed their mind" from "the runner failed"
   in audit logs and in the STATE block's eviction priority.
3. Create a fresh task with ``lineage = [old_id, *old_lineage]`` so
   the audit trail traverses every replan generation.
4. Enqueue the fresh task through the scheduler, surfacing
   ``scheduler_queue_full`` if the cap is saturated.

Lineage propagation is the load-bearing detail: future analytics /
debug views walk the chain to render "this task was replanned 3 times
from <root>".
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


class ReplanTaskArgs(BaseModel):
    """Validated argument shape for ``replan_task`` (v1)."""

    task_id: str = Field(
        ...,
        min_length=1,
        description="ID exact de la sous-tâche à remplacer (depuis le bloc STATE).",
    )
    new_goal: str = Field(
        ...,
        min_length=1,
        description="Nouveau goal pour la tâche de remplacement.",
    )


_REPLAN_DESCRIPTION = (
    "Remplace une sous-tâche en cours par une nouvelle version. "
    "L'ancienne est annulée puis marquée ``superseded``; la nouvelle "
    "hérite du ``lineage`` (chaîne d'ids) pour préserver l'audit. À "
    "utiliser quand l'utilisateur reformule complètement la demande."
)


_REPLAN_PARAMETERS = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "ID exact de la sous-tâche à remplacer (depuis le bloc STATE).",
        },
        "new_goal": {
            "type": "string",
            "description": "Nouveau goal pour la tâche de remplacement.",
        },
    },
    "required": ["task_id", "new_goal"],
}


async def _replan_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Cancel ``task_id``, mark it superseded, spawn a fresh task with lineage."""

    assert isinstance(args, ReplanTaskArgs)
    target_id = args.task_id.strip()
    new_goal = args.new_goal.strip()
    if not target_id:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="task_id is empty after strip",
        )
    if not new_goal:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="new_goal is empty after strip",
        )

    try:
        old_task = ctx.task_store.get_task(target_id)
    except TaskStoreError:
        _logger.warning("orchestrator.replan_unknown_task", task_id=target_id)
        return ToolHandlerOutcome(
            status="error",
            task_id=target_id,
            error_code="unknown_task",
            error_message=f"unknown task: {target_id}",
        )

    # Cancel via the scheduler — this also handles ``spawned`` and
    # ``running`` paths uniformly (slice #0023 / issue 0045).
    await ctx.task_scheduler.cancel(target_id, reason="user_replanned")

    # Then flip to the v2 terminal state. The scheduler's cancel path
    # already transitioned the row to ``failed`` (for non-terminal
    # source states); we override that with ``superseded`` via the
    # injected hook so the audit trail records the right reason.
    if ctx.mark_superseded is not None:
        try:
            ctx.mark_superseded(target_id)
        except Exception:  # pragma: no cover — defensive net.
            _logger.exception(
                "orchestrator.replan_supersede_failed",
                task_id=target_id,
            )

    # Lineage chain: prepend the old id so the audit walks newest →
    # oldest.
    new_lineage = [target_id, *old_task.lineage]
    new_id = ctx.task_store.create_task(
        title=old_task.title,
        goal=new_goal,
        lineage=new_lineage,
        scope=old_task.scope,
    )
    # The replacement starts in ``spawned`` so it counts under the v2
    # queue cap (same pattern as ``spawn_task``).
    try:
        ctx.task_store.update_state(new_id, "spawned")
    except TaskStoreError:
        _logger.warning("replan_task.spawned_transition_failed", task_id=new_id)

    created = ctx.task_store.get_task(new_id)
    emit_debug(
        category="decision",
        severity="info",
        source="orchestrator._dispatch_replan_task",
        summary=f"Jarvis replanifie '{old_task.title}'",
        payload={
            "old_task_id": target_id,
            "new_task_id": new_id,
            "lineage": new_lineage,
        },
    )
    await ctx.ws_emit(
        {
            "type": "task_created",
            "task_id": new_id,
            "title": created.title,
            "goal": created.goal,
            "state": created.state,
            "created_at": created.created_at,
            "lineage": new_lineage,
        }
    )

    try:
        await ctx.task_scheduler.enqueue(new_id)
    except SchedulerQueueFull as exc:
        try:
            ctx.task_store.update_state(new_id, "failed")
            ctx.task_store.set_result(new_id, "scheduler_queue_full")
        except TaskStoreError:
            _logger.exception("replan_task.rollback_failed", task_id=new_id)
        return ToolHandlerOutcome(
            status="error",
            task_id=new_id,
            error_code=SCHEDULER_QUEUE_FULL_ERROR_CODE,
            error_message=str(exc),
        )

    _logger.info(
        "orchestrator.replanned_task",
        old_task_id=target_id,
        new_task_id=new_id,
    )
    return ToolHandlerOutcome(status="ok", task_id=new_id)


def build_replan_task_tool() -> ToolDefinition:
    """Construct the registry entry for ``replan_task`` (v1)."""

    return ToolDefinition(
        name="replan_task",
        version="v1",
        description=_REPLAN_DESCRIPTION,
        parameters=_REPLAN_PARAMETERS,
        args_model=ReplanTaskArgs,
        handler=_replan_handler,
    )
