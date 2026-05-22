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
- On any error (LLM exception, parse failure, unsupported action) the runner
  transitions ``→ failed`` and records a system message describing the
  failure reason. The runner never re-raises — the surrounding
  ``asyncio.create_task`` would otherwise log an unhandled-exception
  warning.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from bob import ws_events
from bob.event_bus import EventBus, get_event_bus
from bob.llm_client import LLMClient
from bob.task_store import Task, TaskStore, TaskStoreError

_logger = structlog.get_logger(__name__)


_SYSTEM_PROMPT_TEMPLATE = (
    "You are a sub-agent. Your goal: {goal}.\n"
    "Emit ONE of these actions as a structured JSON response:\n"
    '  - {{"action": "done", "result": "<result text>"}} when the goal is achieved.\n'
    '  - {{"action": "ask_user", "question": "<question>"}} when you need '
    "clarification from the user (kept minimal — one focused question).\n"
    '  - {{"action": "progress", "status": "<status>"}} for intermediate status '
    "(NOT USED in this slice).\n"
    "Respond with the JSON object ONLY, no markdown fences, no prose around it."
)


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
        """Run the sub-agent for ``task_id``; never re-raises."""

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

        messages = self._build_messages(task)

        try:
            raw = await self._client.chat(messages, session_id=task_id)
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

        # ``progress`` is reserved for a later slice. For now treat it as a
        # protocol violation so we don't silently drop transitions.
        _logger.warning(
            "sub_agent_runner.unsupported_action",
            task_id=task_id,
            action=action,
        )
        await self._fail(task_id, f"action {action!r} not supported in this slice")

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
            self._task_store.append_message(
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

    async def _handle_ask_user(self, task_id: str, question: str) -> None:
        """Persist the question, transition to ``waiting_input``, emit events.

        The runner returns after this — the asyncio task ends naturally, the
        scheduler's done-callback frees the in-memory slot, and the task waits
        in ``waiting_input`` until the orchestrator's ``forward_to_subtask``
        tool re-enqueues it.
        """

        try:
            self._task_store.append_message(
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

        try:
            self._task_store.append_message(task_id, role="system", content=reason)
            self._task_store.update_state(task_id, "failed")
        except TaskStoreError:
            _logger.exception("sub_agent_runner.persist_failed_failed", task_id=task_id)
            return

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.reload_failed_failed", task_id=task_id)
            return

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
