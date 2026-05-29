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

from bob.ui_registry import ComponentDescriptor

#: Current schema version. Every action carries this on the wire so we
#: can later route v1 vs v2 parsers without ambiguity. Bumped in PR.
#: v2 (issue 0065): ``done.ui_payload`` is now the typed :data:`Deliverable`
#: union (markdown string or validated ``ComponentDescriptor``).
SUB_AGENT_SCHEMA_VERSION = 2


#: A document-class deliverable: the finished artefact as a plain markdown
#: string (the shape the model naturally emits for an exposé / report /
#: chronology). Kept as a bare ``str`` so the model is never forced into a
#: wrapper object for what is conceptually just text.
MarkdownDeliverable = str

#: The validated output half of the sub-agent envelope (issue 0065). Either a
#: :data:`MarkdownDeliverable` (markdown string) or a structured
#: :class:`bob.ui_registry.ComponentDescriptor` (``{component, props}``) whose
#: props are validated against the SINGLE ``ui_registry`` component schema by
#: the runner — the same schema the ``say`` tool uses, so the two never drift.
Deliverable = ComponentDescriptor | MarkdownDeliverable


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
    - ``ui_payload``: the deliverable surfaced in the overlay, typed as the
      :data:`Deliverable` union (issue 0065). A markdown string for
      document-class tasks (exposé, report, chronology) or a structured
      :class:`bob.ui_registry.ComponentDescriptor` (``{component, props}``)
      for overlay-class tasks (e.g. a ``Mail`` card). ``None`` when the task
      has no rendered deliverable. Accepting a bare string matches what the
      model naturally emits for a finished document. A descriptor's props are
      validated against the single ``ui_registry`` component schema by the
      runner; an invalid descriptor is routed through the P5 self-correction
      loop (NOT silently dropped).
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
    ui_payload: Deliverable | None = Field(default=None)
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


#: Name carried on the ``response_format`` JSON Schema wrapper (issue 0060).
#: LM Studio echoes it back in errors; a stable name keeps those greppable.
SUB_AGENT_ACTION_SCHEMA_NAME = "sub_agent_action"


def sub_agent_action_response_schema() -> dict[str, Any]:
    """Derive the guided-decoding envelope schema from :data:`SubAgentAction`.

    Issue 0060 (PRD 0008). On a backend that declares ``guided_json`` (LM
    Studio) the sub-agent's control envelope is emitted under
    ``response_format: {"type": "json_schema", …}`` so a fenced / prose-wrapped
    / ``json.loads``-failing envelope is impossible *by construction*. This
    function produces the ``json_schema`` payload — the single source of truth
    is the EXISTING :data:`SubAgentAction` union (``ProgressAction`` /
    ``ToolCallAction`` / ``DoneAction``); we never hand-write a second copy.

    Accommodation (minimal, NOT the general flattener — that is issue 0063):
    Pydantic's ``model_json_schema`` for the discriminated union emits a
    top-level ``oneOf`` + ``$ref`` + ``$defs`` shape, which local /
    OpenAI-compatible guided decoders (vLLM / llama.cpp grammars, LM Studio)
    reject. We therefore project the union onto a single FLAT top-level object
    whose ``action`` property is the discriminator enum (the three literals,
    read off the models themselves) and whose remaining properties are the
    UNION of every branch's own properties. Only ``action`` is ``required`` at
    the envelope level so a ``progress`` reply need not satisfy ``done``'s
    ``status`` / ``reason_code``. The strict per-branch contract (``progress``
    requires ``thought``; ``done`` requires ``status`` + ``reason_code``; the
    closed status enum) is still enforced AFTER decode by :func:`parse_action`
    against the real union — guided decoding gates the shape, ``parse_action``
    gates the branch. This keeps the union the single source: adding a field to
    any action model flows into the envelope here without a second edit.

    One field, ``done.ui_payload``, is the :data:`Deliverable` union (issue
    0065: a markdown string OR a ``ComponentDescriptor``). A union is
    inherently an ``anyOf`` (``+ $ref`` for the descriptor branch) — which the
    guided decoder rejects exactly like the top-level ``oneOf`` — and a flat
    single-object envelope cannot express "string OR object" for one property
    without it. Rather than reproduce a second hand-typed copy (forbidden) or
    narrow the union and lose the markdown-string variant, we DROP any merged
    field whose own schema contains an ``anyOf`` / ``oneOf`` / ``$ref`` from
    the envelope's typed ``properties``. Because ``additionalProperties`` stays
    ``True`` the field is still ACCEPTED on the wire (a ``done`` may still emit
    ``ui_payload``); it is simply not constrained by the grammar. The envelope
    gates the action SHAPE; ``parse_action`` then validates ``ui_payload``
    against the real :data:`Deliverable` union post-decode, and the runner
    validates a descriptor's props against ``ui_registry`` — recovered and
    self-corrected exactly like ``tool_call.args``.

    ``additionalProperties`` stays ``True`` (mirrors the per-action models,
    which carry ``schema_version`` with a default and permissive ``args`` /
    ``cost`` bags) so the constrained decode does not strip a field the union
    happily accepts.
    """

    action_literals: list[str] = []
    merged_properties: dict[str, Any] = {}
    for model in (ProgressAction, ToolCallAction, DoneAction):
        model_schema = model.model_json_schema()
        properties = model_schema.get("properties", {})
        for field_name, field_schema in properties.items():
            if field_name == "action":
                # Read the discriminator literal off the model itself (``const``
                # on Pydantic v2) so the enum cannot drift from the union.
                literal = field_schema.get("const")
                if isinstance(literal, str) and literal not in action_literals:
                    action_literals.append(literal)
                continue
            # Drop fields whose own schema would re-introduce a construct the
            # guided decoder rejects (``done.ui_payload`` is ``anyOf`` today).
            # ``additionalProperties: True`` still admits them on the wire;
            # ``parse_action`` validates them strictly post-decode. This is the
            # minimal accommodation — the general collapser is issue 0063.
            if any(key in field_schema for key in ("anyOf", "oneOf", "$ref")):
                continue
            # First writer wins for shared fields (e.g. ``schema_version``);
            # the per-branch shapes that overlap are identical by construction.
            merged_properties.setdefault(field_name, field_schema)

    envelope_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": action_literals,
                "description": "Discriminator: which of the three actions this is.",
            },
            **merged_properties,
        },
        "required": ["action"],
        "additionalProperties": True,
    }
    return {
        "name": SUB_AGENT_ACTION_SCHEMA_NAME,
        "schema": envelope_schema,
    }


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
    "SUB_AGENT_ACTION_SCHEMA_NAME",
    "SUB_AGENT_SCHEMA_VERSION",
    "Deliverable",
    "DoneAction",
    "MarkdownDeliverable",
    "ProgressAction",
    "SubAgentAction",
    "SubAgentActionParseError",
    "SubAgentDoneStatus",
    "ToolCallAction",
    "parse_action",
    "sub_agent_action_response_schema",
]
