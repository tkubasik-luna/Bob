"""One-shot sub-agent execution.

The orchestrator (slice #0018) spawns a sub-task in :class:`TaskStore` then
schedules a :class:`SubAgentRunner` as a background asyncio task. The runner
loads the task's ``goal``, calls the LLM with a structured-action prompt,
parses the response and persists the outcome.

This slice intentionally supports a *single* action: ``done``. Other actions
(``ask_user``, ``progress``) are mapped to a ``failed`` transition with a
warning log — they land in subsequent slices.

State machine guarantees:

- The task is expected to enter the runner in state ``running`` (the
  orchestrator transitions ``pending → running`` before scheduling).
- On success the runner transitions ``running → done`` and writes the result.
- On any error (LLM exception, parse failure, unsupported action) the runner
  transitions ``running → failed`` and records a system message describing
  the failure reason. The runner never re-raises — the surrounding
  ``asyncio.create_task`` would otherwise log an unhandled-exception warning.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from bob import ws_events
from bob.llm_client import LLMClient
from bob.task_store import TaskStore, TaskStoreError

_logger = structlog.get_logger(__name__)


_SYSTEM_PROMPT_TEMPLATE = (
    "You are a sub-agent. Your goal: {goal}.\n"
    "Emit ONE of these actions as a structured JSON response:\n"
    '  - {{"action": "done", "result": "<result text>"}} when the goal is achieved.\n'
    '  - {{"action": "ask_user", "question": "<question>"}} when you need '
    "clarification (NOT USED in this slice — emit only `done`).\n"
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
    """One-shot LLM call → parse → persist outcome."""

    def __init__(self, *, subagent_client: LLMClient, task_store: TaskStore) -> None:
        self._client = subagent_client
        self._task_store = task_store

    async def run(self, task_id: str) -> None:
        """Run the sub-agent for ``task_id``; never re-raises."""

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.task_not_found", task_id=task_id)
            return

        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(goal=task.goal)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task.goal},
        ]

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

        # Slice #0018 only supports ``done``. Anything else is a parse error
        # from the orchestrator's point of view — log a warning and fail.
        _logger.warning(
            "sub_agent_runner.unsupported_action",
            task_id=task_id,
            action=action,
        )
        await self._fail(task_id, f"action {action!r} not supported in this slice")

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

    async def _fail(self, task_id: str, reason: str) -> None:
        """Mark ``task_id`` as failed with a system message describing ``reason``."""

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
