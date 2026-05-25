"""Versioned :class:`SubAgentAction` schema (PRD 0006 / issue 0045).

A sub-agent's contract surface is intentionally tiny: at every iteration
it emits exactly one of three actions.

- ``progress(thought)`` — a free-form intermediate reflection. Persisted
  to the task message log and surfaced live to any overlay subscriber.
  Does NOT exit the runner loop.
- ``tool_call(name, args)`` — a request to invoke a tool from the
  sub-agent-side registry (see :mod:`bob.sub_agent.tool_registry`).
  The dispatcher executes it, the result is appended to the task
  message log, the runner loops.
- ``done(result_summary, ui_payload?, status, reason_code, cost)`` —
  terminal. ``status`` enumerates how the sub-agent ended (success,
  degraded under a cap, hard failure, cancelled, timeout). ``reason_code``
  is drawn from a versioned :mod:`bob.sub_agent.reason_codes` registry
  (the concrete user-facing copy ships in 0048; the codes we reference
  here already live there). ``cost`` carries tokens / latency / etc.

The schema is intentionally *parsed*, not *emitted*, by the sub-agent
runner. The runner receives a raw JSON action string from the LLM and
validates it through :func:`parse_action`; emitting actions is the LLM's
job, so we only need the inbound parser surface. We expose Pydantic v2
models so future tooling (golden-prompt tests, action recorders) can
re-validate the same shapes without re-deriving them from the LLM
prompt.

The current ``schema_version`` is ``1``. Bumping it is a deliberate,
PR-reviewable lever — if the LLM-facing action surface changes shape we
expect the runner to gate on the version on the way in.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError

#: Current schema version. Every action carries this on the wire so we
#: can later route v1 vs v2 parsers without ambiguity. Bumped in PR.
SUB_AGENT_SCHEMA_VERSION = 1


#: Closed set of terminal statuses the sub-agent can report on ``done``.
#: ``complete`` means "goal reached cleanly". ``degraded`` means "the
#: sub-agent finished under a cap (iteration / wall-clock / token) but
#: produced an answer". ``failed`` means the run could not produce a
#: usable answer. ``cancelled`` means the orchestrator (or the user via
#: 0050's cancel_task tool) interrupted us cooperatively before the
#: 2 s grace expired. ``timeout`` means the wall-clock budget elapsed.
SubAgentDoneStatus = Literal[
    "complete",
    "degraded",
    "failed",
    "cancelled",
    "timeout",
]


class ProgressAction(BaseModel):
    """Free-form intermediate reflection — does NOT terminate the run.

    ``thought`` mirrors the legacy ``progress.status`` field but the
    keyword changes to make clear this is a reflection emitted to the
    overlay (event refactor 0052 will subscribe to it), not a user-
    visible status line.
    """

    action: Literal["progress"]
    thought: str = Field(..., min_length=1)
    schema_version: int = Field(default=SUB_AGENT_SCHEMA_VERSION)


class ToolCallAction(BaseModel):
    """Request to invoke a tool from the sub-agent-side registry.

    ``name`` must match a :class:`bob.sub_agent.tool_registry.SubAgentToolDefinition`
    name. ``args`` is a free-form dict validated by the tool's own
    Pydantic args model at dispatch time — we keep it permissive here so
    the runner can route the dispatch error through ``done(failed,
    invalid_output)`` in 0048.
    """

    action: Literal["tool_call"]
    name: str = Field(..., min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = Field(default=SUB_AGENT_SCHEMA_VERSION)


class DoneAction(BaseModel):
    """Terminal action — exits the runner loop.

    Carries everything later slices need to render the result:

    - ``result_summary``: short prose, surfaced to Jarvis in the
      ``task_completed`` ContextEntry (0046 already wires this).
    - ``ui_payload``: optional dict — the markdown overlay payload
      0050 / 0052 will render. Free-form for now (no client schema
      yet).
    - ``status``: see :data:`SubAgentDoneStatus`.
    - ``reason_code``: short code drawn from the :mod:`bob.sub_agent.reason_codes`
      registry. Codes used at this slice are intentionally minimal —
      ``ok``, ``iteration_cap``, ``wall_clock_cap``, ``token_cap``,
      ``user_cancelled``, ``hard_killed``, ``invalid_output``,
      ``llm_failed``. The actual i18n shipped to the frontend lands
      in 0048.
    - ``cost``: free-form dict carrying tokens / latency / etc. We
      intentionally keep it permissive so future fields (cache hit
      rate, retries) land without a schema bump.
    """

    action: Literal["done"]
    result_summary: str = Field(default="")
    ui_payload: dict[str, Any] | None = Field(default=None)
    status: SubAgentDoneStatus
    reason_code: str = Field(..., min_length=1)
    cost: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = Field(default=SUB_AGENT_SCHEMA_VERSION)


#: Discriminated union of the three actions. Pydantic v2's
#: ``Field(discriminator="action")`` makes parse-time errors point at
#: the correct branch instead of a sea of "anyOf" mismatches.
SubAgentAction = Annotated[
    ProgressAction | ToolCallAction | DoneAction,
    Field(discriminator="action"),
]


class SubAgentActionEnvelope(BaseModel):
    """Adapter object used purely to drive Pydantic's discriminator.

    Pydantic does not let us validate a top-level discriminated union
    directly; wrapping it in an envelope keeps the discriminator while
    letting us call ``model_validate`` on the raw dict the runner
    decoded from the LLM. The envelope is never persisted — it exists
    only to surface validation errors against the right action branch.
    """

    inner: SubAgentAction


class SubAgentActionParseError(ValueError):
    """Raised when a sub-agent action payload cannot be parsed.

    The runner catches this and converts it into a forced
    ``done(status=failed, reason_code=invalid_output)`` so the upstream
    contract stays "the runner always emits a terminal action".
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_action(payload: dict[str, Any]) -> ProgressAction | ToolCallAction | DoneAction:
    """Validate a raw action dict against the v1 schema.

    Pydantic's discriminated-union validation pinpoints which branch
    failed (``"action='unknown'"`` → invalid literal, etc.). Errors are
    folded into :class:`SubAgentActionParseError` so call sites match
    on our contract rather than on Pydantic's version.
    """

    if not isinstance(payload, dict):
        raise SubAgentActionParseError(
            f"top-level payload must be an object, got {type(payload).__name__}"
        )
    try:
        envelope = SubAgentActionEnvelope.model_validate({"inner": payload})
    except ValidationError as exc:
        raise SubAgentActionParseError(str(exc)) from exc
    return envelope.inner


__all__ = [
    "SUB_AGENT_SCHEMA_VERSION",
    "DoneAction",
    "ProgressAction",
    "SubAgentAction",
    "SubAgentActionParseError",
    "SubAgentDoneStatus",
    "ToolCallAction",
    "parse_action",
]
