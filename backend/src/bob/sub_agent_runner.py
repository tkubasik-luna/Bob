"""Multi-turn sub-agent execution.

The orchestrator (slice #0018) spawns a sub-task in :class:`TaskStore` then
schedules a :class:`SubAgentRunner` as a background asyncio task. The runner
loads the task's ``goal``, calls the LLM with a structured-action prompt,
parses the response and persists the outcome.

Slice #0021 extends the runner to support ``ask_user`` in addition to
``done``: when the LLM emits ``ask_user`` the task transitions to
``waiting_input`` and the runner returns cleanly. The orchestrator's
``forward_to_subtask`` tool later re-enqueues the task with the user's
answer appended to its message log; ``run`` is then re-invoked and re-enters
the loop with the full history in scope.

Slice #0022 extends the runner with ``progress``: the sub-agent may emit
``progress(status)`` zero or more times to surface a live intermediate
status to the sidebar without terminating the task. A single ``run`` call
loops until the LLM emits a terminal action (``done`` / ``ask_user``); a
hard cap of :data:`MAX_PROGRESS_ITERATIONS` consecutive ``progress`` emits
without a terminal transitions the task to ``failed`` with reason
``max_iterations_exceeded`` to prevent infinite loops. ``progress`` does
NOT publish on ``task_state_changed`` (state stays ``running``), so the
:class:`ProactivityHandler` never fires on progress and Jarvis stays
silent in the main chat.

Slice #0023 adds cancellation: :class:`TaskScheduler` can call
``runner_task.cancel()`` on the asyncio handle. The cancellation point is
typically the ``await self._client.chat(...)`` inside the loop. The runner
re-raises :class:`asyncio.CancelledError` so the scheduler's done-callback
observes a cancelled task; the scheduler then owns the ``running → failed``
transition + reason persistence. The runner does NOT attempt any further
state writes after cancellation — that would race the scheduler.

State machine guarantees:

- A task can enter the runner in state ``pending`` (just spawned, slot free)
  or ``waiting_input`` (resumed after a user forward). In both cases the
  runner transitions to ``running`` before issuing the LLM call. When the
  scheduler already promoted ``pending → running`` (slot-free fast path)
  the runner detects that and skips the redundant transition.
- On ``done`` the runner transitions ``running → done`` and writes the result.
- On ``ask_user`` the runner appends the question to the message log,
  transitions ``running → waiting_input`` and returns. The EventBus
  publishes ``task_state_changed`` so the proactivity handler can react.
- On ``progress`` the runner appends the status to the message log, emits a
  ``task_updated`` event carrying ``progress_status`` (state stays
  ``running``), publishes ``task_message_added`` on the bus, and re-iterates.
  No ``task_state_changed`` event — the state did not change.
- On any error (LLM exception, parse failure, unsupported action, progress
  cap exceeded) the runner transitions ``→ failed`` and records a system
  message describing the failure reason. The runner never re-raises — the
  surrounding ``asyncio.create_task`` would otherwise log an
  unhandled-exception warning. ``asyncio.CancelledError`` is the one
  exception that DOES propagate (slice #0023).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from bob import ws_events
from bob.debug_log import current_task_id, emit_debug, start_task
from bob.event_bus import EventBus, get_event_bus
from bob.llm_client import LLMClient
from bob.task_store import Task, TaskStore, TaskStoreError

_logger = structlog.get_logger(__name__)


def _task_title(store: TaskStore, task_id: str) -> str:
    """Best-effort title lookup for debug summaries — never raises."""

    try:
        return store.get_task(task_id).title
    except TaskStoreError:
        return task_id


_SYSTEM_PROMPT_TEMPLATE = (
    "You are a sub-agent. Your goal: {goal}.\n"
    "Emit ONE of these actions as a structured JSON response:\n"
    '  - {{"action": "done", "result": "<result text>"}} when the goal is achieved.\n'
    '  - {{"action": "ask_user", "question": "<question>"}} when you need '
    "clarification from the user (kept minimal — one focused question).\n"
    '  - {{"action": "progress", "status": "<status>"}} to surface an '
    'intermediate status (e.g. "analysing document 3 of 10"). Use sparingly '
    "for long-running work; the loop will re-invoke you immediately so always "
    "follow up with done or ask_user once the work is complete.\n"
    "Respond with the JSON object ONLY, no markdown fences, no prose around it."
)

# Hard cap on consecutive ``progress`` emits without a terminal action
# (``done`` / ``ask_user``). Beyond this the runner fails the task with
# reason ``max_iterations_exceeded`` to prevent infinite progress loops.
MAX_PROGRESS_ITERATIONS = 10


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence around a JSON payload.

    Mirrors :func:`bob.llm_client._strip_code_fence` — kept local so the
    runner does not depend on a private symbol.
    """

    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    first = lines[0].lstrip("`").strip().lower()
    if first not in ("", "json"):
        return text
    body_end = len(lines)
    if lines[-1].strip().startswith("```"):
        body_end -= 1
    return "\n".join(lines[1:body_end]).strip()


class _ParseError(RuntimeError):
    """Internal marker for malformed sub-agent payloads."""


def _parse_action(raw_text: str) -> dict[str, Any]:
    """Decode + validate the sub-agent's JSON action.

    Raises :class:`_ParseError` on any structural issue (decode failure,
    unknown action, missing required field, wrong field type).
    """

    payload_text = _strip_code_fence(raw_text)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise _ParseError(
            f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(payload, dict):
        raise _ParseError(f"top-level JSON value must be an object, got {type(payload).__name__}")

    action = payload.get("action")
    if action not in ("done", "ask_user", "progress"):
        raise _ParseError(f"unknown action: {action!r}")

    if action == "done":
        result = payload.get("result")
        if not isinstance(result, str):
            raise _ParseError("`done` action requires a string `result` field")
        return {"action": "done", "result": result}

    if action == "ask_user":
        question = payload.get("question")
        if not isinstance(question, str):
            raise _ParseError("`ask_user` action requires a string `question` field")
        return {"action": "ask_user", "question": question}

    # action == "progress"
    status = payload.get("status")
    if not isinstance(status, str):
        raise _ParseError("`progress` action requires a string `status` field")
    return {"action": "progress", "status": status}


class SubAgentRunner:
    """LLM call → parse → persist outcome (supports multi-turn via ``ask_user``)."""

    def __init__(
        self,
        *,
        subagent_client: LLMClient,
        task_store: TaskStore,
        event_bus: EventBus | None = None,
    ) -> None:
        self._client = subagent_client
        self._task_store = task_store
        # ``None`` means "resolve the singleton on demand" — see _bus property.
        # We capture the bound bus (if any) on the way in to support tests
        # that wire a dedicated bus instance.
        self._explicit_bus = event_bus

    @property
    def _bus(self) -> EventBus:
        return self._explicit_bus if self._explicit_bus is not None else get_event_bus()

    async def run(self, task_id: str) -> None:
        """Run the sub-agent for ``task_id``; never re-raises.

        Iterates within a single call to support the ``progress`` action: the
        LLM may emit progress events back-to-back, and only ``done`` /
        ``ask_user`` exit the loop. A consecutive-progress cap of
        :data:`MAX_PROGRESS_ITERATIONS` guards against infinite loops.
        """

        # Slice 0043: install ``task_id`` in the ``current_task_id`` ContextVar
        # for the lifetime of this ``run`` so every ``emit_debug`` triggered
        # inside (this method, the LLM client, the progress/done/ask_user/fail
        # handlers, anything spawned via ``asyncio.create_task``) inherits the
        # id as ``parent_task_id``. The reset-token guarantees correct nesting
        # if a sub-task itself triggers another sub-task in the same context.
        token = start_task(task_id)
        try:
            await self._run(task_id)
        finally:
            current_task_id.reset(token)

    async def _run(self, task_id: str) -> None:
        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.task_not_found", task_id=task_id)
            return

        # The scheduler is expected to have transitioned the task to ``running``
        # before scheduling us. We accept ``running`` (fresh spawn or resume
        # from forward_to_subtask) and reject anything else loudly — that's a
        # contract violation between the scheduler and the runner.
        if task.state != "running":
            _logger.warning(
                "sub_agent_runner.unexpected_state",
                task_id=task_id,
                state=task.state,
            )
            return

        progress_count = 0
        while True:
            # Reload the task on each iteration so the message log is fresh
            # for the next LLM call (progress entries get persisted between
            # turns and must be replayed back to the model).
            try:
                task = self._task_store.get_task(task_id)
            except TaskStoreError:
                _logger.exception("sub_agent_runner.task_reload_failed", task_id=task_id)
                return

            messages = self._build_messages(task)

            try:
                raw = await self._client.chat(messages, session_id=task_id)
            except asyncio.CancelledError:
                # Slice #0023: the scheduler cancelled us via
                # ``runner_task.cancel()``. Re-raise so the asyncio task
                # ends with ``cancelled() is True`` and the scheduler's
                # done-callback observes it. The scheduler owns the
                # ``running → failed`` transition + reason persistence —
                # we must NOT call self._fail here (it would write a system
                # message and emit events the scheduler is about to emit
                # itself with the proper reason).
                raise
            except Exception as exc:
                _logger.exception("sub_agent_runner.llm_failed", task_id=task_id)
                await self._fail(task_id, f"LLM call failed: {exc}")
                return

            try:
                action_payload = _parse_action(raw)
            except _ParseError as exc:
                _logger.warning(
                    "sub_agent_runner.parse_failed",
                    task_id=task_id,
                    reason=str(exc),
                    raw_preview=raw[:200],
                )
                await self._fail(task_id, f"sub-agent response invalid: {exc}")
                return

            action = action_payload["action"]
            if action == "done":
                await self._handle_done(task_id, action_payload["result"])
                return

            if action == "ask_user":
                await self._handle_ask_user(task_id, action_payload["question"])
                return

            if action == "progress":
                progress_count += 1
                if progress_count > MAX_PROGRESS_ITERATIONS:
                    _logger.warning(
                        "sub_agent_runner.progress_cap_exceeded",
                        task_id=task_id,
                        cap=MAX_PROGRESS_ITERATIONS,
                    )
                    await self._fail(task_id, "max_iterations_exceeded")
                    return
                await self._handle_progress(task_id, action_payload["status"])
                continue

            # Defensive — ``_parse_action`` already filters unknown actions,
            # but mypy needs the explicit branch.
            _logger.warning(
                "sub_agent_runner.unsupported_action",
                task_id=task_id,
                action=action,
            )
            await self._fail(task_id, f"action {action!r} not supported")
            return

    def _build_messages(self, task: Task) -> list[dict[str, Any]]:
        """Build the LLM message list, including any prior turns persisted in the log.

        On a fresh spawn the log is empty (the goal is in ``task.goal``); on a
        resume the log contains the previous ``ask_user`` (role=assistant,
        action=ask_user) plus the user's forwarded reply (role=user). We
        replay all of it so the model can continue from where it left off.
        """

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT_TEMPLATE.format(goal=task.goal)},
            {"role": "user", "content": task.goal},
        ]
        # Append the persisted message log. Keep ``system`` rows out — those
        # are internal failure-reason annotations and the LLM does not need
        # them. The runner stores ``ask_user`` questions as assistant rows
        # and forwarded answers as user rows, which round-trip naturally.
        try:
            for msg in self._task_store.get_task_messages(task.id):
                if msg.role == "system":
                    continue
                messages.append({"role": msg.role, "content": msg.content})
        except TaskStoreError:
            _logger.exception("sub_agent_runner.history_load_failed", task_id=task.id)
        return messages

    async def _handle_done(self, task_id: str, result: str) -> None:
        try:
            self._task_store.set_result(task_id, result)
            message_id = self._task_store.append_message(
                task_id, role="assistant", content=result, action="done"
            )
            self._task_store.update_state(task_id, "done")
        except TaskStoreError:
            _logger.exception("sub_agent_runner.persist_done_failed", task_id=task_id)
            return

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.reload_done_failed", task_id=task_id)
            return

        emit_debug(
            category="task",
            severity="info",
            source="bob.sub_agent_runner._handle_done",
            summary=f"Sub-task '{task.title}' terminée",
            payload={
                "task_id": task_id,
                "title": task.title,
                "result": result,
            },
        )

        await _emit_task_message(
            self._task_store,
            task_id,
            message_id=message_id,
        )
        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": task.state,
                "needs_attention": task.needs_attention,
                "updated_at": task.updated_at,
            }
        )
        await ws_events.emit(
            {
                "type": "task_result",
                "task_id": task_id,
                "result": result,
            }
        )
        await self._bus.publish(
            "task_state_changed",
            {
                "task_id": task_id,
                "old_state": "running",
                "new_state": "done",
                "action": "done",
            },
        )

    async def _handle_progress(self, task_id: str, status: str) -> None:
        """Persist a progress status, emit WS events, leave state ``running``.

        ``task_state_changed`` is intentionally NOT published: the state did
        not change. The :class:`ProactivityHandler` is subscribed only to
        ``task_state_changed`` so it stays silent on progress events —
        Jarvis does not surface intermediate statuses in the main chat.

        ``task_message_added`` IS published so future subscribers (and tests)
        can observe progress emissions on the bus without coupling to the
        WS layer.
        """

        try:
            message_id = self._task_store.append_message(
                task_id, role="assistant", content=status, action="progress"
            )
        except TaskStoreError:
            _logger.exception("sub_agent_runner.persist_progress_failed", task_id=task_id)
            return

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.reload_progress_failed", task_id=task_id)
            return

        emit_debug(
            category="task",
            severity="debug",
            source="bob.sub_agent_runner._handle_progress",
            summary=f"Sub-task '{task.title}' progresse: {status}",
            payload={
                "task_id": task_id,
                "title": task.title,
                "status": status,
            },
        )

        await _emit_task_message(
            self._task_store,
            task_id,
            message_id=message_id,
        )
        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": task.state,
                "needs_attention": task.needs_attention,
                "updated_at": task.updated_at,
                "progress_status": status,
            }
        )
        # Bus notification — explicitly NOT ``task_state_changed``: state
        # did not transition, so the proactivity handler must not fire.
        await self._bus.publish(
            "task_message_added",
            {
                "task_id": task_id,
                "message_id": message_id,
                "role": "assistant",
                "action": "progress",
            },
        )

    async def _handle_ask_user(self, task_id: str, question: str) -> None:
        """Persist the question, transition to ``waiting_input``, emit events.

        The runner returns after this — the asyncio task ends naturally, the
        scheduler's done-callback frees the in-memory slot, and the task waits
        in ``waiting_input`` until the orchestrator's ``forward_to_subtask``
        tool re-enqueues it.
        """

        try:
            message_id = self._task_store.append_message(
                task_id, role="assistant", content=question, action="ask_user"
            )
            self._task_store.update_state(task_id, "waiting_input")
        except TaskStoreError:
            _logger.exception("sub_agent_runner.persist_ask_user_failed", task_id=task_id)
            return

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.reload_ask_user_failed", task_id=task_id)
            return

        emit_debug(
            category="task",
            severity="info",
            source="bob.sub_agent_runner._handle_ask_user",
            summary=f"Sub-task '{task.title}' demande user input",
            payload={
                "task_id": task_id,
                "title": task.title,
                "question": question,
            },
        )

        await _emit_task_message(
            self._task_store,
            task_id,
            message_id=message_id,
        )
        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": task.state,
                "needs_attention": task.needs_attention,
                "updated_at": task.updated_at,
            }
        )
        await self._bus.publish(
            "task_state_changed",
            {
                "task_id": task_id,
                "old_state": "running",
                "new_state": "waiting_input",
                "action": "ask_user",
            },
        )

    async def _fail(self, task_id: str, reason: str) -> None:
        """Mark ``task_id`` as failed with a system message describing ``reason``."""

        # Capture the pre-failure state so we can report it accurately on the
        # bus. We tolerate the row being gone — failing-on-the-way-out is
        # idempotent.
        try:
            previous = self._task_store.get_task(task_id).state
        except TaskStoreError:
            previous = "running"

        # Capture title before we transition: easier to access here than
        # post-update and the title doesn't change with state.
        title = _task_title(self._task_store, task_id)

        try:
            message_id = self._task_store.append_message(task_id, role="system", content=reason)
            self._task_store.update_state(task_id, "failed")
        except TaskStoreError:
            _logger.exception("sub_agent_runner.persist_failed_failed", task_id=task_id)
            return

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.reload_failed_failed", task_id=task_id)
            return

        emit_debug(
            category="task",
            severity="warn",
            source="bob.sub_agent_runner._fail",
            summary=f"Sub-task '{title}' a échoué: {reason}",
            payload={
                "task_id": task_id,
                "title": title,
                "reason": reason,
                "previous_state": previous,
            },
        )

        await _emit_task_message(
            self._task_store,
            task_id,
            message_id=message_id,
        )
        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": task.state,
                "needs_attention": task.needs_attention,
                "updated_at": task.updated_at,
            }
        )
        await ws_events.emit(
            {
                "type": "task_result",
                "task_id": task_id,
                "result": reason,
            }
        )
        await self._bus.publish(
            "task_state_changed",
            {
                "task_id": task_id,
                "old_state": previous,
                "new_state": "failed",
            },
        )


async def _emit_task_message(store: TaskStore, task_id: str, *, message_id: int) -> None:
    """Push a ``task_message`` WS event for a freshly-appended task message.

    Used by the runner and the orchestrator to surface live transcript
    updates so an open drawer can append the new line without re-fetching
    the whole snapshot. We re-read the row to pick up the SQL DEFAULT
    ``created_at`` and the ``role`` / ``action`` exactly as stored —
    avoids drift between what the caller passed in and what the DB
    persisted.
    """

    try:
        for msg in store.get_task_messages(task_id):
            if msg.id != message_id:
                continue
            await ws_events.emit(
                {
                    "type": "task_message",
                    "task_id": task_id,
                    "message_id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "action": msg.action,
                    "created_at": msg.created_at,
                }
            )
            return
    except TaskStoreError:
        _logger.exception("sub_agent_runner.emit_task_message_lookup_failed", task_id=task_id)
