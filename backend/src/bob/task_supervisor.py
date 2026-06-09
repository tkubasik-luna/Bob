"""Supervision of fire-and-forget asyncio tasks (PRD 0018 / issue 0124).

A bare ``asyncio.create_task(coro)`` whose handle is never awaited is a silent
failure mode: an exception raised inside the coroutine is stored on the task
and only surfaces as a cryptic "Task exception was never retrieved" warning at
garbage-collection time — long after the user observed the symptom (a proactive
announcement that was never voiced, a dead proactive flusher, ...).

:func:`supervise` attaches a done-callback that ALWAYS consumes the task
result and, when the task failed, reports it twice:

- a structured ERROR log (``task_supervisor.task_failed``) carrying the task
  name + whatever context the spawn site provided (session, turn/msg id, ...);
- a ``system``/``error`` debug event so the failure is visible live on
  ``/ws/debug`` and lands in ``orchestration.jsonl`` for offline diagnosis.

A cancelled task is a normal outcome (interruption, barge-in, shutdown) and
reports nothing. The callback itself never raises.

:func:`create_supervised_task` is the one-line replacement for the common
``asyncio.create_task(...)`` + manual bookkeeping pattern at fire-and-forget
spawn sites (TTS synthesis, event-bus subscriber dispatch, the orchestrator's
proactive flusher / typing reset).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import structlog

from bob.debug_log import emit_debug

_logger = structlog.get_logger(__name__)


def supervise[T](
    task: asyncio.Task[T],
    *,
    name: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    msg_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> asyncio.Task[T]:
    """Attach the result-consuming done-callback to ``task``; return it.

    ``name`` identifies the spawn site (e.g. ``tts.proactive_synthesis``).
    ``session_id`` / ``turn_id`` / ``msg_id`` are the standard correlation ids;
    ``context`` carries any extra site-specific fields (e.g. the event-bus
    topic). All of them land verbatim in the log line and the debug event
    payload so a failure is diagnosable without reproducing it.
    """

    def _on_done(done: asyncio.Task[T]) -> None:
        if done.cancelled():
            return
        # ``exception()`` consumes the stored exception, so asyncio never logs
        # "Task exception was never retrieved" for a supervised task.
        exc = done.exception()
        if exc is None:
            return
        payload: dict[str, Any] = {
            "task_name": name,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if session_id is not None:
            payload["session_id"] = session_id
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if msg_id is not None:
            payload["msg_id"] = msg_id
        if context:
            payload.update(context)
        _logger.error("task_supervisor.task_failed", exc_info=exc, **payload)
        emit_debug(
            category="system",
            severity="error",
            source="bob.task_supervisor",
            summary=f"Tâche de fond « {name} » en échec: {type(exc).__name__}: {exc}",
            payload=payload,
            turn_id=turn_id,
        )

    task.add_done_callback(_on_done)
    return task


def create_supervised_task[T](
    coro: Coroutine[Any, Any, T],
    *,
    name: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    msg_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> asyncio.Task[T]:
    """``asyncio.create_task`` + :func:`supervise` in one call.

    The asyncio task is also named ``name`` so it shows up identifiably in
    ``asyncio.all_tasks()`` dumps / debug tooling.
    """

    return supervise(
        asyncio.create_task(coro, name=name),
        name=name,
        session_id=session_id,
        turn_id=turn_id,
        msg_id=msg_id,
        context=context,
    )


__all__ = ["create_supervised_task", "supervise"]
