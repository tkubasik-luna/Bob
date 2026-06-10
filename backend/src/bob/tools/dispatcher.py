"""Single-point :class:`ToolDispatcher` driven by a :class:`ToolRegistry`.

The dispatcher is the *only* path the orchestrator uses to execute a
Jarvis tool call. It serves three goals (PRD 0006, issue 0044):

1. **Centralise validation.** Argument shape is checked with each tool's
   Pydantic model before the handler runs. Unknown tool name and
   Pydantic failures both surface as a structured
   :class:`DispatchResult(outcome="error", ...)` — the orchestrator
   currently surfaces those the same way the legacy code did (the turn
   falls through to the chat path). The actual retry/degrade behavior
   on validation errors is wired in 0048.
2. **Emit a structured ``jarvis.route`` event on every dispatch.** Issue
   0044 (and PRD 0006 user story #19) makes "why did Jarvis chat
   instead of spawning?" debuggable by grepping for one event source
   instead of parsing prose. The event payload carries the tool name,
   the version, the outcome and, on errors, the error code + the
   redacted argument dict (so PII does not leak verbatim into logs).
3. **Stay handler-agnostic.** Individual tool handlers under
   :mod:`bob.tools.definitions` receive a small :class:`ToolHandlerContext`
   DI bag (task store, scheduler, ws emit). The dispatcher knows nothing
   about what a tool does; it only orchestrates lookup, validation,
   invocation and the route event.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ValidationError

from bob.debug_log import DebugSeverity, emit_debug
from bob.llm.types import ToolCall
from bob.tools.registry import (
    ToolArgsValidationError,
    ToolDefinition,
    ToolRegistry,
)
from bob.tools.types import ToolHandlerOutcome
from bob.validation.policy import RetryPolicy, get_policy

if TYPE_CHECKING:  # pragma: no cover — typing-only.
    from bob.sub_agent.addendum_queue import AddendumQueue

_logger = structlog.get_logger(__name__)


#: Outcome literal carried on every :class:`DispatchResult`.
DispatchOutcome = Literal["ok", "error"]


#: Canonical ``source`` string for the structured ``jarvis.route`` debug
#: events emitted by :class:`ToolDispatcher`. Centralised so tests and
#: greps can pin to one literal.
JARVIS_ROUTE_EVENT_SOURCE = "jarvis.route"


class _WsEmitterLike(Protocol):
    async def __call__(self, event: dict[str, Any]) -> None: ...


class _SchedulerLike(Protocol):
    async def enqueue(self, task_id: str) -> None: ...

    async def resume(self, task_id: str) -> None: ...

    async def cancel(self, task_id: str, *, reason: str = ...) -> None: ...


class _TaskStoreLike(Protocol):
    """Narrow protocol used by tool handlers.

    Kept here (rather than imported from :mod:`bob.task_store`) so the
    test harness can plug a recording double without dragging the SQLite
    plumbing into the import graph of registry-only tests.

    Issue 0050 extends the surface with ``update_state``,
    ``set_result``, ``list_tasks``, ``set_delivered_at_turn`` and
    ``mark_superseded`` so the v2 task tools (and the orchestrator's
    completion debouncer) can transition rows without reaching past
    the protocol into the concrete :class:`TaskStore`.
    """

    def create_task(
        self,
        *,
        title: str,
        goal: str,
        parent_task_id: str | None = ...,
        lineage: Any = ...,
        scope: Any = ...,
    ) -> str: ...

    def get_task(self, task_id: str) -> Any: ...

    def list_tasks(self, *, state: Any = ..., limit: Any = ...) -> Any: ...

    def append_message(
        self,
        task_id: str,
        *,
        role: Any,
        content: str,
        action: Any = ...,
    ) -> int: ...

    def get_task_messages(self, task_id: str) -> Any: ...

    def update_state(self, task_id: str, new_state: Any) -> None: ...

    def set_result(self, task_id: str, result: str) -> None: ...

    def set_delivered_at_turn(self, task_id: str, turn_index: int) -> None: ...

    def mark_superseded(self, task_id: str) -> None: ...

    def find_by_query(
        self,
        query: str,
        *,
        prefer_state: Any = ...,
        limit: int = ...,
    ) -> Any: ...


class _JarvisStoreLike(Protocol):
    """Narrow protocol used by the ``say`` tool handler.

    Issue 0047 routes Jarvis direct replies through the unified ``say``
    tool. The handler persists the assistant turn so subsequent context
    assembly sees the reply in history — owning the persistence in the
    handler (rather than the orchestrator) keeps the dispatcher path
    fully self-contained: every successful turn ends with exactly one
    handler call that performs both the side effect and the persistence.
    """

    def append(self, role: Any, content: str, action: Any = ...) -> None: ...


@dataclass(frozen=True)
class ToolHandlerContext:
    """Dependencies handed to every tool handler.

    Centralising this here (rather than threading individual references
    through each handler) keeps individual tool files in
    :mod:`bob.tools.definitions` lean: they import the context type and
    nothing else from the orchestrator. Adding a new dependency (e.g.
    the event bus when 0052 lands) is a one-line change here and a
    no-op for handlers that don't need it.

    Issue 0047 adds the optional ``jarvis_store`` so the unified ``say``
    tool can persist its assistant turn through the same DI bag as every
    other handler.

    Issue 0050 adds:

    * ``addendum_queue_factory`` — resolves the per-task
      :class:`bob.sub_agent.addendum_queue.AddendumQueue` for a live
      runner so the ``addendum_task`` tool can push info into a
      running sub-agent without restarting it. The factory returns
      ``None`` when the task is not currently running (the dispatcher
      then surfaces a structured ``task_not_running`` error).
    * ``mark_superseded`` — explicit hook the ``replan_task`` tool
      calls to flip the previous task to the v2 terminal state after
      the scheduler cancelled it. Threaded through DI so unit tests
      can pass a no-op when they don't care about the side effect.

    All new fields default to ``None`` so legacy registry / dispatcher
    contract tests keep compiling without a stub.
    """

    task_store: _TaskStoreLike
    task_scheduler: _SchedulerLike
    ws_emit: _WsEmitterLike
    jarvis_store: _JarvisStoreLike | None = None
    addendum_queue_factory: Callable[[str], AddendumQueue | None] | None = None
    mark_superseded: Callable[[str], None] | None = None


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of one :meth:`ToolDispatcher.dispatch` call.

    Fields:

    - ``outcome`` — ``"ok"`` when the tool handler returned ``status="ok"``;
      ``"error"`` for every failure path (unknown tool, Pydantic
      validation failure, handler-reported error like
      ``"task_not_waiting_input"``).
    - ``tool_name`` / ``tool_version`` — populated whenever the
      registry could resolve the call (so even validation errors carry
      provenance). ``tool_version`` is ``None`` for unknown tools.
    - ``task_id`` — the task id touched by a successful call (created,
      resumed, cancelled). ``None`` for the unified ``say`` tool (issue
      0047), and ``None`` for every error branch.
    - ``error_code`` / ``error_message`` — populated on ``outcome="error"``.
      ``error_code`` follows the ``reason_code`` shape used elsewhere in
      PRD 0006 (``"unknown_tool"``, ``"invalid_args"``, ``"unknown_task"``,
      ``"task_not_waiting_input"``, ``"handler_failed"``).
    - ``speech`` / ``ui`` — populated by the unified ``say`` tool (issue
      0047) so the orchestrator can lift the spoken text + optional UI
      payload into the :class:`OrchestratorResponse`. ``None`` for every
      other tool and for every error branch.
    """

    outcome: DispatchOutcome
    tool_name: str
    tool_version: str | None = None
    task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    speech: str | None = None
    ui: Any | None = None

    @property
    def ok(self) -> bool:
        """Convenience flag — ``True`` iff the dispatch succeeded."""

        return self.outcome == "ok"


class ToolDispatcher:
    """Validate + execute :class:`ToolCall` instances against a :class:`ToolRegistry`."""

    def __init__(self, registry: ToolRegistry, context: ToolHandlerContext) -> None:
        self._registry = registry
        self._context = context

    @property
    def registry(self) -> ToolRegistry:
        """Expose the underlying registry for callers that need to inspect it.

        The orchestrator uses this to project the LLM-facing tool list.
        """

        return self._registry

    async def dispatch(self, call: ToolCall) -> DispatchResult:
        """Run one tool call end-to-end.

        Pipeline:

        1. Look up ``call.name`` in the registry. Miss → ``"unknown_tool"``
           error result + ``jarvis.route`` event.
        2. Validate ``call.arguments`` with ``definition.args_model``.
           Failure → ``"invalid_args"`` error result + event. When the
           tool's :class:`RetryPolicy` enables ``accept_partial`` we first
           drop keys unknown to the model and retry validation against
           the required-only subset; only a missing-or-malformed
           *required* field still fails.
        3. Invoke ``definition.handler`` with a context + validated args.
           The handler may itself return ``ToolHandlerOutcome(status="error",
           ...)`` for domain-level failures (unknown task id, wrong state).
        4. Emit one ``jarvis.route`` event with the final outcome and
           return the :class:`DispatchResult`.

        The dispatcher never raises on bad input; every error path is
        modelled as a :class:`DispatchResult(outcome="error", ...)` so
        the orchestrator branches on shape, not on exception types.
        """

        definition = self._registry.get(call.name)
        if definition is None:
            result = DispatchResult(
                outcome="error",
                tool_name=call.name,
                tool_version=None,
                error_code="unknown_tool",
                error_message=f"unknown tool: {call.name}",
            )
            self._emit_route_event(result, arguments=call.arguments)
            _logger.warning(
                "tool_dispatcher.unknown_tool",
                tool_name=call.name,
            )
            return result

        try:
            validated = self._validate_args(definition, call.arguments)
        except ToolArgsValidationError as exc:
            result = DispatchResult(
                outcome="error",
                tool_name=definition.name,
                tool_version=definition.version,
                error_code="invalid_args",
                error_message=exc.message,
            )
            self._emit_route_event(result, arguments=call.arguments)
            _logger.warning(
                "tool_dispatcher.invalid_args",
                tool_name=definition.name,
                error=exc.message,
            )
            return result

        try:
            outcome = await definition.handler(self._context, validated)
        except Exception as exc:  # pragma: no cover — defensive net.
            # A handler should not raise; if it does we fold the exception
            # into the same shape so the orchestrator code path stays the
            # same. The exception is logged with a stack trace so the
            # underlying bug stays visible.
            _logger.exception(
                "tool_dispatcher.handler_raised",
                tool_name=definition.name,
            )
            result = DispatchResult(
                outcome="error",
                tool_name=definition.name,
                tool_version=definition.version,
                error_code="handler_failed",
                error_message=str(exc) or exc.__class__.__name__,
            )
            self._emit_route_event(result, arguments=call.arguments)
            return result

        result = self._result_from_handler_outcome(definition, outcome)
        self._emit_route_event(result, arguments=call.arguments)
        return result

    def _validate_args(self, definition: ToolDefinition, arguments: dict[str, Any]) -> BaseModel:
        """Validate ``arguments`` against ``definition.args_model``.

        We convert Pydantic's :class:`ValidationError` into our
        :class:`ToolArgsValidationError` so call sites pattern-match on
        the registry's contract, not on Pydantic's version-specific
        error type. The original error message is preserved verbatim so
        debugging stays trivial.

        Issue 0048 adds the per-tool ``accept_partial`` lever: when the
        first validation fails AND :attr:`RetryPolicy.accept_partial` is
        true for this tool, we strip keys that are not part of the
        model's field set and retry validation. If the required-field
        subset is valid the call succeeds first try, saving a network
        round-trip on the common "valid required + garbage optional"
        case (the unified ``say`` tool sees this whenever the LLM emits
        ``emotion`` / ``tone`` / ``confidence`` under temperature).
        """

        try:
            return definition.args_model.model_validate(arguments)
        except ValidationError as exc:
            policy = self._policy_for(definition.name)
            if not policy.accept_partial:
                raise ToolArgsValidationError(
                    tool_name=definition.name,
                    message=str(exc),
                ) from exc
            # Strip keys unknown to the model and retry validation. We
            # do *not* call :func:`model_construct` here because that
            # would bypass field-level validators; the goal is "valid
            # required + dropped optionals", not "skip validation".
            allowed_keys = set(definition.args_model.model_fields.keys())
            pruned = {k: v for k, v in arguments.items() if k in allowed_keys}
            if pruned == arguments:
                # Nothing was unknown — the validation error is about
                # required-field shape, not extra keys. Re-raise.
                raise ToolArgsValidationError(
                    tool_name=definition.name,
                    message=str(exc),
                ) from exc
            try:
                return definition.args_model.model_validate(pruned)
            except ValidationError as inner_exc:
                raise ToolArgsValidationError(
                    tool_name=definition.name,
                    message=str(inner_exc),
                ) from inner_exc

    @staticmethod
    def _policy_for(tool_name: str) -> RetryPolicy:
        """Look up the :class:`RetryPolicy` for ``tool_name``.

        Indirection-as-method so tests can monkeypatch the lookup
        without reaching into :mod:`bob.validation.policy` globals.
        """

        return get_policy(tool_name)

    def _result_from_handler_outcome(
        self,
        definition: ToolDefinition,
        outcome: ToolHandlerOutcome,
    ) -> DispatchResult:
        """Convert a handler outcome into the dispatcher-facing result."""

        if outcome.status == "ok":
            return DispatchResult(
                outcome="ok",
                tool_name=definition.name,
                tool_version=definition.version,
                task_id=outcome.task_id,
                speech=outcome.speech,
                ui=outcome.ui,
            )
        return DispatchResult(
            outcome="error",
            tool_name=definition.name,
            tool_version=definition.version,
            task_id=outcome.task_id,
            error_code=outcome.error_code or "handler_failed",
            error_message=outcome.error_message,
        )

    def _emit_route_event(
        self,
        result: DispatchResult,
        *,
        arguments: dict[str, Any],
    ) -> None:
        """Push the canonical ``jarvis.route`` structured debug event.

        Payload shape (stable across slices — PRD 0006 user story #19):

        - ``tool``: the LLM-facing tool name.
        - ``version``: tool definition version (``None`` for unknown
          tools so the field is always present).
        - ``outcome``: ``"ok"`` / ``"error"``.
        - ``task_id``: populated for ``ok`` and for error branches that
          carry a task id (``unknown_task``, ``task_not_waiting_input``).
        - ``error_code`` / ``error_message``: populated on error.
        - ``argument_keys``: keys of the call argument dict so the event
          conveys "what was the LLM trying to do" without leaking the
          full payload verbatim. Values intentionally omitted — sensitive
          content (forwarded user replies, …) should not land in the
          debug ring buffer.
        """

        severity: DebugSeverity = "info" if result.outcome == "ok" else "warn"
        summary = (
            f"jarvis.route {result.tool_name} → {result.outcome}"
            if result.tool_version is None
            else f"jarvis.route {result.tool_version}.{result.tool_name} → {result.outcome}"
        )
        emit_debug(
            category="decision",
            severity=severity,
            source=JARVIS_ROUTE_EVENT_SOURCE,
            summary=summary,
            payload={
                "tool": result.tool_name,
                "version": result.tool_version,
                "outcome": result.outcome,
                "task_id": result.task_id,
                "error_code": result.error_code,
                "error_message": result.error_message,
                "argument_keys": sorted(arguments.keys()),
            },
        )
