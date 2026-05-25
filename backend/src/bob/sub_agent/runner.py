"""Multi-turn sub-agent execution — v2 contract (PRD 0006 / issue 0045).

Replaces the slice #0018..#0023 monolithic runner with a structured
loop honouring:

- a versioned :class:`bob.sub_agent.actions.SubAgentAction` schema
  with three actions (``progress``, ``tool_call``, ``done``);
- a centralised :class:`bob.sub_agent.policy.SubAgentPolicy` for the
  iteration / wall-clock / token caps + per-task-type overrides;
- an :class:`asyncio.Queue`-backed
  :class:`bob.sub_agent.addendum_queue.AddendumQueue` per task, drained
  ONLY at iteration boundaries;
- cooperative cancellation checkpoints at iteration boundary AND
  tool-call boundary;
- a 2 s grace timeout after cancel followed by a hard-kill fallback
  (driven by :meth:`asyncio.Task.cancel` from the scheduler under its
  shared :class:`asyncio.TaskGroup`).

Cancellation contract
---------------------

The runner exposes a single :meth:`SubAgentRunner.request_cancel`
method which sets a cooperative flag. The next checkpoint (between
iterations, or before a tool dispatch) detects it, persists a forced
``done(status=cancelled, reason_code=user_cancelled)``, and exits.

If the runner does not reach a checkpoint within
``policy.cancel_grace_seconds`` (default ``2 s``), the scheduler (the
caller) is expected to escalate via :meth:`asyncio.Task.cancel` on the
runner's asyncio handle — that fires :class:`asyncio.CancelledError`
inside whatever ``await`` the runner is parked on (typically the LLM
call). The runner converts the ``CancelledError`` into a forced
``done(status=cancelled, reason_code=hard_killed)`` so the task store
still ends with a terminal ``done`` row.

The contract between the runner and the scheduler is:

- The scheduler runs each :class:`SubAgentRunner` instance inside a
  single :class:`asyncio.TaskGroup` shared across the scheduler. If
  the orchestrator crashes the TaskGroup's ``__aexit__`` cleans up
  every in-flight runner deterministically — no leaked background
  coroutines (PRD 0006 user story #24).
- The scheduler invokes :meth:`request_cancel` first, awaits
  ``policy.cancel_grace_seconds`` and only then escalates to
  :meth:`asyncio.Task.cancel`. The runner records both paths
  distinctly via the ``reason_code`` on the final ``done`` action so
  downstream consumers (0052 events overlay) can show
  ``user_cancelled`` vs ``hard_killed`` to the dev.

Cap behaviour
-------------

The three global caps each force a terminal action with a specific
``reason_code``:

- ``max_iterations`` exceeded → ``done(degraded, iteration_cap)``;
- ``wall_clock_seconds`` exceeded → ``done(timeout, wall_clock_cap)``;
- ``token_cap`` exceeded → ``done(degraded, token_cap)``.

Token usage is accumulated from the prompt + completion estimates of
each LLM call. Until the streaming LLM client (0049) reports real
token counts, we approximate via the same ``len(text) // 4`` heuristic
:mod:`bob.llm_client` already uses for debug summaries.

Wall clock is measured via a clock callable (defaults to
:func:`time.monotonic`) so tests can inject deterministic time without
real ``asyncio.sleep`` calls.

Legacy compatibility
--------------------

The runner accepts BOTH the legacy ``{"action": "done", "result": …}``
shape (slice #0018..#0023, used by current orchestrator tests) and the
new versioned shape (``result_summary``/``status``/``reason_code``/
``cost``). Internally everything normalises to the v1
:class:`SubAgentAction` envelope: a legacy ``done`` becomes
``done(status="complete", reason_code="ok", cost={})`` so the schema
contract holds on the way out.

The ``ask_user`` action from slice #0021 is preserved as a side
channel: the runner detects an ``ask_user`` payload and transitions
the task to ``waiting_input`` (consumers like the proactivity handler
still observe the ``task_state_changed`` bus event). 0050 will
replace ``ask_user`` with the v2 ``addendum_task`` flow; until then
the legacy behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from bob import ws_events
from bob.context.prompt_fragments import (
    SUB_AGENT_V2_ADDENDUM_TEMPLATE,
    SUB_AGENT_V2_SYSTEM_PROMPT,
)
from bob.debug_log import current_task_id, emit_debug, start_task
from bob.event_bus import EventBus, get_event_bus
from bob.llm_client import LLMClient
from bob.sub_agent.actions import (
    SUB_AGENT_SCHEMA_VERSION,
    DoneAction,
    ProgressAction,
    SubAgentActionParseError,
    SubAgentDoneStatus,
    ToolCallAction,
    parse_action,
)
from bob.sub_agent.addendum_queue import AddendumEntry, AddendumQueue
from bob.sub_agent.policy import SubAgentPolicy, default_policy
from bob.sub_agent.tool_registry import (
    SubAgentToolDispatcher,
    SubAgentToolRegistry,
    build_default_subagent_registry,
)
from bob.task_store import Task, TaskStore, TaskStoreError
from bob.validation import (
    SUB_AGENT_DEFAULT_POLICY,
    CallEnvelope,
    ExhaustedContext,
    OnValidationExhausted,
    SubAgentOnValidationExhausted,
    build_validator_message,
    render_feedback,
)
from bob.validation.reason_codes import (
    REASON_HARD_KILLED,
    REASON_INVALID_OUTPUT,
    REASON_ITERATION_CAP,
    REASON_LLM_FAILED,
    REASON_OK,
    REASON_TOKEN_CAP,
    REASON_TOOL_FAILED,
    REASON_USER_CANCELLED,
    REASON_WALL_CLOCK_CAP,
)

_logger = structlog.get_logger(__name__)


# --- Reason codes ------------------------------------------------------------
#
# Issue 0048 moves the reason-code literals into
# :mod:`bob.validation.reason_codes` so the registry is the single source
# of truth. We re-export the names from this module so existing call sites
# importing them from :mod:`bob.sub_agent.runner` keep working.


#: Clock callable used to read wall-clock time. Defaults to
#: :func:`time.monotonic`. Tests inject a controllable clock so cap
#: behaviours are deterministic without sleeping.
Clock = Callable[[], float]


def _estimate_tokens_text(text: str) -> int:
    """Rough heuristic — ~4 chars/token (matches ``bob.llm_client``)."""

    return len(text) // 4


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += _estimate_tokens_text(content)
    return total


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence around a JSON payload."""

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


@dataclass(frozen=True)
class _NormalisedPayload:
    """Internal: an LLM action payload normalised against the v1 schema.

    Carries either a :class:`SubAgentAction` variant (parsed via the
    versioned schema) or a synthetic ``ask_user`` payload preserved for
    the legacy flow. Exactly one of the fields is populated.
    """

    action: ProgressAction | ToolCallAction | DoneAction | None = None
    ask_user_question: str | None = None


def _normalise_payload(raw: str) -> _NormalisedPayload:
    """Normalise an LLM response string into a v1 action payload.

    Accepts the new shape directly (validated via :func:`parse_action`)
    and the legacy ``done``/``ask_user``/``progress`` shape by mapping
    the legacy keys into v1 ones. Raises
    :class:`SubAgentActionParseError` when the payload is unrecognisable
    so the runner converts it to a forced
    ``done(failed, invalid_output)``.
    """

    payload_text = _strip_code_fence(raw)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise SubAgentActionParseError(
            f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(payload, dict):
        raise SubAgentActionParseError(
            f"top-level JSON value must be an object, got {type(payload).__name__}"
        )

    action = payload.get("action")
    if action == "ask_user":
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            raise SubAgentActionParseError("``ask_user`` requires a non-empty ``question`` string")
        return _NormalisedPayload(ask_user_question=question)

    # ``progress`` legacy uses ``status``; v1 uses ``thought``. Same for
    # ``done`` (``result`` legacy / ``result_summary`` v1) — translate
    # before validating so the v1 parser accepts either shape.
    if action == "progress" and "thought" not in payload and "status" in payload:
        payload = {**payload, "thought": payload["status"]}
        payload.pop("status", None)

    if action == "done":
        translated = dict(payload)
        if "result_summary" not in translated:
            legacy_result = translated.pop("result", None)
            if not isinstance(legacy_result, str):
                # Neither the new ``result_summary`` field nor the legacy
                # ``result`` field is present — treat as parse error so
                # the runner forces ``done(failed, invalid_output)``.
                # Matches slice #0018..#0023 behaviour: a ``done`` payload
                # without a result string was always a parse failure.
                raise SubAgentActionParseError(
                    "`done` requires `result_summary` (or legacy `result`) string"
                )
            translated["result_summary"] = legacy_result
        if "status" not in translated:
            translated["status"] = "complete"
        if "reason_code" not in translated:
            translated["reason_code"] = REASON_OK
        if "cost" not in translated:
            translated["cost"] = {}
        payload = translated

    parsed = parse_action(payload)
    return _NormalisedPayload(action=parsed)


class SubAgentRunner:
    """Runs a single sub-agent task to terminal ``done``.

    Construct one per task (the scheduler does this through its
    runner factory). The runner is single-shot: :meth:`run` should be
    invoked exactly once for a given task id.

    Dependencies are pinned via the constructor so the runner is fully
    DI'd: tests pass scriptable clients, custom registries, deterministic
    clocks. The boot path uses :func:`default_policy` /
    :func:`build_default_subagent_registry`.
    """

    def __init__(
        self,
        *,
        subagent_client: LLMClient,
        task_store: TaskStore,
        event_bus: EventBus | None = None,
        policy: SubAgentPolicy | None = None,
        tool_registry: SubAgentToolRegistry | None = None,
        addendum_queue: AddendumQueue | None = None,
        clock: Clock | None = None,
        on_validation_exhausted: OnValidationExhausted | None = None,
    ) -> None:
        self._client = subagent_client
        self._task_store = task_store
        self._explicit_bus = event_bus
        self._policy = policy or default_policy()
        self._tool_registry = tool_registry or build_default_subagent_registry()
        self._tool_dispatcher = SubAgentToolDispatcher(self._tool_registry)
        self._addendum_queue = addendum_queue or AddendumQueue()
        self._clock = clock or time.monotonic
        # Cooperative cancel flag set by :meth:`request_cancel`. Read at
        # every checkpoint (iteration boundary, pre-tool-call). The runner
        # also handles :class:`asyncio.CancelledError` raised by the
        # scheduler's hard-kill path.
        self._cancel_requested = False
        # Track whether the runner observed a hard-kill (CancelledError)
        # so the terminal done can report ``hard_killed`` vs
        # ``user_cancelled``. Tests assert on this through the task row.
        self._hard_killed = False
        # PRD 0006 / issue 0048 — validation degrade contract. The
        # default handler delegates to ``force_failed_invalid_output``
        # so the forced ``done(failed, invalid_output)`` keeps the
        # existing finalisation path (lineage preservation + bus event).
        self._on_validation_exhausted: OnValidationExhausted = (
            on_validation_exhausted or SubAgentOnValidationExhausted(runner=self)
        )

    @property
    def addendum_queue(self) -> AddendumQueue:
        """Expose the per-task addendum queue for producers (0050)."""

        return self._addendum_queue

    @property
    def policy(self) -> SubAgentPolicy:
        return self._policy

    def request_cancel(self) -> None:
        """Set the cooperative cancellation flag.

        Picked up at the next iteration or pre-tool-call checkpoint.
        Schedule callers should set this, await
        ``policy.cancel_grace_seconds`` and only then escalate via
        :meth:`asyncio.Task.cancel`.
        """

        self._cancel_requested = True

    @property
    def _bus(self) -> EventBus:
        return self._explicit_bus if self._explicit_bus is not None else get_event_bus()

    async def run(self, task_id: str) -> None:
        """Run the sub-agent for ``task_id``; never re-raises (except CancelledError handling).

        Wraps :meth:`_run` in the ``current_task_id`` ContextVar so
        every :func:`emit_debug` triggered inside inherits the id as
        ``parent_task_id`` (slice 0043 contract).
        """

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

        if task.state != "running":
            _logger.warning(
                "sub_agent_runner.unexpected_state",
                task_id=task_id,
                state=task.state,
            )
            return

        started_at = self._clock()
        iteration = 0
        tokens_used = 0
        pending_addenda: list[AddendumEntry] = []
        # PRD 0006 / issue 0048 — validation feedback messages re-injected
        # on the next LLM call under the ``system_validator`` role. Reset
        # on every successful parse so a stale retry buffer cannot leak
        # across iteration boundaries.
        validator_feedback: list[dict[str, Any]] = []
        validator_envelope = CallEnvelope(tool_name=None, actor="sub_agent")

        while True:
            # ---- Checkpoint 1: iteration boundary -----------------------
            if self._cancel_requested:
                await self._emit_terminal_done(
                    task_id,
                    status="cancelled",
                    reason_code=REASON_USER_CANCELLED,
                    result_summary="",
                    cost=self._build_cost(
                        started_at=started_at,
                        iterations=iteration,
                        tokens_used=tokens_used,
                    ),
                )
                return

            if iteration >= self._policy.max_iterations:
                await self._emit_terminal_done(
                    task_id,
                    status="degraded",
                    reason_code=REASON_ITERATION_CAP,
                    result_summary="",
                    cost=self._build_cost(
                        started_at=started_at,
                        iterations=iteration,
                        tokens_used=tokens_used,
                    ),
                )
                return

            elapsed = self._clock() - started_at
            if elapsed >= self._policy.wall_clock_seconds:
                await self._emit_terminal_done(
                    task_id,
                    status="timeout",
                    reason_code=REASON_WALL_CLOCK_CAP,
                    result_summary="",
                    cost=self._build_cost(
                        started_at=started_at,
                        iterations=iteration,
                        tokens_used=tokens_used,
                    ),
                )
                return

            if tokens_used >= self._policy.token_cap:
                await self._emit_terminal_done(
                    task_id,
                    status="degraded",
                    reason_code=REASON_TOKEN_CAP,
                    result_summary="",
                    cost=self._build_cost(
                        started_at=started_at,
                        iterations=iteration,
                        tokens_used=tokens_used,
                    ),
                )
                return

            # Drain the addendum queue exactly here — at the iteration
            # boundary, before building the next LLM prompt. 0050 fills
            # the queue; today the drain is a no-op for production use.
            drained = self._addendum_queue.drain()
            pending_addenda.extend(drained)
            # Issue 0052: each drained addendum surfaces in the per-task
            # overlay as an ``addendum_received`` reflection event so
            # the dev can see exactly when a user enrichment landed in
            # the sub-agent's prompt.
            for entry in drained:
                emit_debug(
                    category="task",
                    severity="info",
                    source="bob.sub_agent_runner.run",
                    summary=f"Addendum reçu: {entry.text[:80]}",
                    payload={
                        "task_id": task_id,
                        "kind": "addendum_received",
                        "text": entry.text,
                    },
                )

            try:
                task = self._task_store.get_task(task_id)
            except TaskStoreError:
                _logger.exception("sub_agent_runner.task_reload_failed", task_id=task_id)
                return

            messages = self._build_messages(task, pending_addenda)
            # Consume the addenda once they have been folded into the
            # prompt — they should not appear on the next iteration too.
            pending_addenda = []

            # Re-inject any pending validator feedback under the dedicated
            # ``system_validator`` role (issue 0048). Empty list on the
            # happy path — only populated after a parse failure inside
            # the retry budget.
            if validator_feedback:
                messages = [*messages, *validator_feedback]

            try:
                raw = await self._client.chat(messages, session_id=task_id)
            except asyncio.CancelledError:
                # Hard-kill from the scheduler. Mark the path so the
                # terminal ``done`` records ``hard_killed``. Then convert
                # the CancelledError into a clean terminal done — the
                # scheduler's TaskGroup expects every runner to finish
                # naturally so the group's __aexit__ does not have to
                # absorb a CancelledError per child.
                self._hard_killed = True
                with contextlib.suppress(Exception):
                    await self._emit_terminal_done(
                        task_id,
                        status="cancelled",
                        reason_code=REASON_HARD_KILLED,
                        result_summary="",
                        cost=self._build_cost(
                            started_at=started_at,
                            iterations=iteration,
                            tokens_used=tokens_used,
                        ),
                    )
                # Re-raise so the scheduler observes the cancellation on
                # its asyncio.Task. The done-callback path uses this to
                # free the slot. Inside a TaskGroup, this propagates as a
                # cancelled child task — the group keeps draining siblings.
                raise
            except Exception as exc:
                _logger.exception("sub_agent_runner.llm_failed", task_id=task_id)
                await self._emit_terminal_done(
                    task_id,
                    status="failed",
                    reason_code=REASON_LLM_FAILED,
                    result_summary=f"LLM call failed: {exc}",
                    cost=self._build_cost(
                        started_at=started_at,
                        iterations=iteration,
                        tokens_used=tokens_used,
                    ),
                )
                return

            tokens_used += _estimate_messages_tokens(messages) + _estimate_tokens_text(raw)

            try:
                normalised = _normalise_payload(raw)
            except SubAgentActionParseError as exc:
                _logger.warning(
                    "sub_agent_runner.parse_failed",
                    task_id=task_id,
                    reason=str(exc),
                    raw_preview=raw[:200],
                    attempt=validator_envelope.attempts,
                )
                # Issue 0048 — instead of immediately failing the task
                # on the first invalid output we feed escaped validator
                # feedback back to the LLM under the
                # ``system_validator`` role and retry. The retry counter
                # rides on the transient :class:`CallEnvelope` (never
                # persisted). Budget exhaustion routes through the
                # shared ``on_validation_exhausted`` handler which calls
                # back into ``force_failed_invalid_output``.
                policy = SUB_AGENT_DEFAULT_POLICY
                if validator_envelope.retries_used >= policy.max_retries:
                    await self._on_validation_exhausted.on_validation_exhausted(
                        ExhaustedContext(
                            envelope=validator_envelope,
                            last_error_message=f"sub-agent response invalid: {exc}",
                            task_id=task_id,
                        )
                    )
                    return
                validator_feedback.append(
                    build_validator_message(
                        render_feedback(
                            error_message=(
                                "Ta dernière sortie est invalide: "
                                f"{exc}. Ré-essaye en émettant exactement "
                                "UN objet JSON conforme au schéma."
                            ),
                            offending_raw=raw,
                        )
                    )
                )
                validator_envelope.record_feedback(validator_feedback[-1]["content"])
                validator_envelope.increment(error_code=REASON_INVALID_OUTPUT)
                continue

            # Successful parse — drop any pending validator feedback so
            # it doesn't bleed into the next iteration's prompt.
            validator_feedback = []
            validator_envelope = CallEnvelope(tool_name=None, actor="sub_agent")

            if normalised.ask_user_question is not None:
                # Legacy ask_user path — preserved until 0050 replaces it
                # with the v2 addendum flow.
                await self._handle_ask_user(task_id, normalised.ask_user_question)
                return

            assert normalised.action is not None  # mypy
            action = normalised.action

            if isinstance(action, DoneAction):
                await self._handle_done(task_id, action)
                return

            if isinstance(action, ProgressAction):
                iteration += 1
                await self._handle_progress(task_id, action.thought)
                continue

            if isinstance(action, ToolCallAction):
                # ---- Checkpoint 2: tool-call boundary -------------------
                if self._cancel_requested:
                    await self._emit_terminal_done(
                        task_id,
                        status="cancelled",
                        reason_code=REASON_USER_CANCELLED,
                        result_summary="",
                        cost=self._build_cost(
                            started_at=started_at,
                            iterations=iteration,
                            tokens_used=tokens_used,
                        ),
                    )
                    return
                iteration += 1
                await self._handle_tool_call(task_id, action)
                continue

            # Defensive — the discriminated union has no other branches.
            _logger.error(
                "sub_agent_runner.unsupported_action",
                task_id=task_id,
                action=type(action).__name__,
            )
            await self._emit_terminal_done(
                task_id,
                status="failed",
                reason_code=REASON_INVALID_OUTPUT,
                result_summary=f"action {type(action).__name__} not supported",
                cost=self._build_cost(
                    started_at=started_at,
                    iterations=iteration,
                    tokens_used=tokens_used,
                ),
            )
            return

    # --- Message + prompt building -------------------------------------------

    def _build_messages(
        self,
        task: Task,
        pending_addenda: list[AddendumEntry],
    ) -> list[dict[str, Any]]:
        """Build the LLM message list including history + drained addenda."""

        tool_lines = "\n".join(
            f"- ``{definition.name}`` : {definition.description}"
            for definition in self._tool_registry
        )
        system_prompt = SUB_AGENT_V2_SYSTEM_PROMPT.render(goal=task.goal)
        if tool_lines:
            system_prompt += "\n\nOutils disponibles :\n" + tool_lines

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task.goal},
        ]
        try:
            for msg in self._task_store.get_task_messages(task.id):
                if msg.role == "system":
                    continue
                messages.append({"role": msg.role, "content": msg.content})
        except TaskStoreError:
            _logger.exception("sub_agent_runner.history_load_failed", task_id=task.id)

        for entry in pending_addenda:
            messages.append(
                {
                    "role": "user",
                    "content": SUB_AGENT_V2_ADDENDUM_TEMPLATE.render(text=entry.text),
                }
            )

        return messages

    # --- Action handlers -----------------------------------------------------

    async def _handle_done(self, task_id: str, action: DoneAction) -> None:
        """Persist a v2 ``done`` action coming from the LLM."""

        await self._finalize_done(
            task_id,
            status=action.status,
            reason_code=action.reason_code,
            result_summary=action.result_summary,
            ui_payload=action.ui_payload,
            cost=action.cost,
        )

    async def _handle_progress(self, task_id: str, thought: str) -> None:
        """Persist a v2 ``progress`` thought, leave state ``running``."""

        try:
            message_id = self._task_store.append_message(
                task_id, role="assistant", content=thought, action="progress"
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
            summary=f"Sub-task '{task.title}' progresse: {thought}",
            payload={
                "task_id": task_id,
                "title": task.title,
                "thought": thought,
                # Issue 0052: explicit reflection kind so the per-task
                # overlay can render a coloured pill / icon per category
                # without payload sniffing.
                "kind": "thought",
                "schema_version": SUB_AGENT_SCHEMA_VERSION,
            },
        )

        await _emit_task_message(self._task_store, task_id, message_id=message_id)
        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": task.state,
                "needs_attention": task.needs_attention,
                "updated_at": task.updated_at,
                "progress_status": thought,
            }
        )
        await self._bus.publish(
            "task_message_added",
            {
                "task_id": task_id,
                "message_id": message_id,
                "role": "assistant",
                "action": "progress",
            },
        )

    async def _handle_tool_call(self, task_id: str, action: ToolCallAction) -> None:
        """Execute a sub-agent-side tool call and persist its outcome.

        The dispatcher returns a structured result. We log the call as
        an ``assistant`` message (the LLM's intent) and the result as a
        ``tool`` message (the response, which round-trips into the next
        iteration's prompt). 0048's retry policy will adjust this path
        — today every dispatch error round-trips as a tool message so
        the LLM can try again.
        """

        call_payload = json.dumps({"action": "tool_call", "name": action.name, "args": action.args})
        try:
            self._task_store.append_message(
                task_id,
                role="assistant",
                content=call_payload,
            )
        except TaskStoreError:
            _logger.exception("sub_agent_runner.persist_tool_call_failed", task_id=task_id)
            return

        # Issue 0052: emit a ``tool_invoke`` reflection BEFORE the
        # dispatch so the overlay can show "calling X..." while the
        # tool is still running.
        emit_debug(
            category="task",
            severity="debug",
            source="bob.sub_agent_runner._handle_tool_call",
            summary=f"Sub-task appelle outil {action.name}",
            payload={
                "task_id": task_id,
                "tool": action.name,
                "args": action.args,
                "kind": "tool_invoke",
            },
        )

        result = await self._tool_dispatcher.dispatch(
            name=action.name,
            arguments=action.args,
            context=_RuntimeToolContext(task_id=task_id),
        )

        body: dict[str, Any]
        if result.ok:
            body = {"status": "ok", "result": result.result}
        else:
            body = {
                "status": "error",
                "error_code": result.error_code,
                "error_message": result.error_message,
            }
        try:
            self._task_store.append_message(
                task_id,
                role="tool",
                content=json.dumps({"tool": action.name, **body}),
            )
        except TaskStoreError:
            _logger.exception("sub_agent_runner.persist_tool_result_failed", task_id=task_id)
            return

        emit_debug(
            category="task",
            severity="info" if result.ok else "warn",
            source="bob.sub_agent_runner._handle_tool_call",
            summary=f"Sub-task tool {action.name} → {result.outcome}",
            payload={
                "task_id": task_id,
                "tool": action.name,
                "outcome": result.outcome,
                "error_code": result.error_code,
                # Issue 0052 — paired with the preceding ``tool_invoke``.
                "kind": "tool_result",
            },
        )

    async def _handle_ask_user(self, task_id: str, question: str) -> None:
        """Legacy ``ask_user`` flow preserved until 0050 replaces it."""

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
                # Issue 0052: ask_user is a status transition into
                # ``waiting_input`` — surface it as the same reflection
                # kind the overlay handles.
                "kind": "status_change",
                "new_state": "waiting_input",
            },
        )
        await _emit_task_message(self._task_store, task_id, message_id=message_id)
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

    async def force_failed_invalid_output(
        self,
        *,
        task_id: str,
        error_message: str,
    ) -> None:
        """Forced terminal ``done(failed, invalid_output)`` (PRD 0006 / issue 0048).

        Entry point used by :class:`SubAgentOnValidationExhausted` when
        the validator runs out of retries on a malformed LLM payload.
        Goes through :meth:`_finalize_done` so the lineage / bus event /
        task_result WS frame remain identical to a regular failure.
        """

        await self._finalize_done(
            task_id,
            status="failed",
            reason_code=REASON_INVALID_OUTPUT,
            result_summary=f"sub-agent response invalid: {error_message}",
            ui_payload=None,
            cost={},
        )

    async def _finalize_done(
        self,
        task_id: str,
        *,
        status: SubAgentDoneStatus,
        reason_code: str,
        result_summary: str,
        ui_payload: dict[str, Any] | None,
        cost: dict[str, Any],
    ) -> None:
        """Persist the terminal state + emit WS / bus events.

        ``status in {complete, degraded}`` → task row state ``done`` with
        ``result_summary`` recorded as ``task.result``.
        ``status in {failed, cancelled, timeout}`` → task row state
        ``failed`` with the ``result_summary`` (or the reason code if
        empty) recorded as both a ``system`` message and ``task.result``
        so the existing ``task_result`` WS event still surfaces a string.

        Idempotent against races: if the row is already terminal we
        skip silently (the scheduler may have already finalised a
        cancel).
        """

        try:
            current = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.finalize_reload_failed", task_id=task_id)
            return
        if current.state in ("done", "failed"):
            return

        store_state: str
        if status in ("complete", "degraded"):
            store_state = "done"
            persisted_result = result_summary
        else:
            store_state = "failed"
            persisted_result = result_summary or reason_code

        # ``done`` rows record ``result`` before the state flips so
        # subscribers see a consistent snapshot. ``failed`` / ``cancelled``
        # / ``timeout`` rows persist the reason as a ``system`` message
        # only — keeping ``task.result is None`` mirrors the legacy
        # ``_fail`` semantics (slice #0018..#0023) so the existing tests
        # don't drift. The scheduler's own cancel path still calls
        # ``set_result`` separately when the user supplies a reason
        # string.
        try:
            if store_state == "done":
                self._task_store.set_result(task_id, persisted_result)
                message_id = self._task_store.append_message(
                    task_id,
                    role="assistant",
                    content=persisted_result,
                    action="done",
                )
                self._task_store.update_state(task_id, "done")
            else:
                message_id = self._task_store.append_message(
                    task_id,
                    role="system",
                    content=persisted_result,
                )
                self._task_store.update_state(task_id, "failed")
        except TaskStoreError:
            _logger.exception("sub_agent_runner.finalize_persist_failed", task_id=task_id)
            return

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception("sub_agent_runner.finalize_reload_done_failed", task_id=task_id)
            return

        emit_debug(
            category="task",
            severity="info" if store_state == "done" else "warn",
            source=(
                "bob.sub_agent_runner._handle_done"
                if store_state == "done"
                else "bob.sub_agent_runner._fail"
            ),
            summary=(
                f"Sub-task '{task.title}' terminée"
                if store_state == "done"
                else f"Sub-task '{task.title}' a échoué: {reason_code}"
            ),
            payload={
                "task_id": task_id,
                "title": task.title,
                "result": persisted_result if store_state == "done" else None,
                "reason": reason_code if store_state != "done" else None,
                "status": status,
                "reason_code": reason_code,
                "ui_payload": ui_payload,
                "cost": cost,
                # Issue 0052: status_change reflection so the overlay
                # can render a terminal pill in the timeline.
                "kind": "status_change",
                "new_state": store_state,
                "schema_version": SUB_AGENT_SCHEMA_VERSION,
            },
        )

        await _emit_task_message(self._task_store, task_id, message_id=message_id)
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
                "result": persisted_result,
            }
        )
        await self._bus.publish(
            "task_state_changed",
            {
                "task_id": task_id,
                "old_state": "running",
                "new_state": store_state,
                "action": "done",
                "status": status,
                "reason_code": reason_code,
            },
        )

    async def _emit_terminal_done(
        self,
        task_id: str,
        *,
        status: SubAgentDoneStatus,
        reason_code: str,
        result_summary: str,
        cost: dict[str, Any],
    ) -> None:
        """Convenience wrapper around :meth:`_finalize_done` for cap paths."""

        await self._finalize_done(
            task_id,
            status=status,
            reason_code=reason_code,
            result_summary=result_summary,
            ui_payload=None,
            cost=cost,
        )

    def _build_cost(
        self,
        *,
        started_at: float,
        iterations: int,
        tokens_used: int,
    ) -> dict[str, Any]:
        return {
            "iterations": iterations,
            "tokens_estimate": tokens_used,
            "elapsed_seconds": max(0.0, self._clock() - started_at),
        }


@dataclass(frozen=True)
class _RuntimeToolContext:
    """Concrete context handed to sub-agent tool handlers.

    Conforms structurally to :class:`SubAgentToolHandlerContext` (a
    :class:`typing.Protocol`); we deliberately do not inherit from the
    Protocol class so the dataclass field can coexist with the Protocol's
    ``task_id`` property declaration without ``no setter`` clashes.

    Currently exposes only the ``task_id`` and a free-form ``state``
    dict — placeholder definitions don't read either, but tests can
    plug a custom registry that does.
    """

    task_id: str

    @property
    def state(self) -> dict[str, Any]:
        return {}


async def _emit_task_message(
    store: TaskStore,
    task_id: str,
    *,
    message_id: int,
) -> None:
    """Push a ``task_message`` WS event for a freshly-appended task message."""

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


__all__ = [
    "REASON_HARD_KILLED",
    "REASON_INVALID_OUTPUT",
    "REASON_ITERATION_CAP",
    "REASON_LLM_FAILED",
    "REASON_OK",
    "REASON_TOKEN_CAP",
    "REASON_TOOL_FAILED",
    "REASON_USER_CANCELLED",
    "REASON_WALL_CLOCK_CAP",
    "SubAgentRunner",
]
