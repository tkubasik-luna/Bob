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
from typing import Any, TypeGuard

import structlog
from pydantic import ValidationError

from bob import ws_events
from bob.context.prompt_fragments import (
    SUB_AGENT_V2_ADDENDUM_TEMPLATE,
    SUB_AGENT_V2_SYSTEM_PROMPT,
    select_skill_packs,
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
    sub_agent_action_response_schema,
)
from bob.sub_agent.addendum_queue import AddendumEntry, AddendumQueue
from bob.sub_agent.policy import SubAgentPolicy, default_policy
from bob.sub_agent.result_store import StoredResult, ToolResultStore
from bob.sub_agent.tool_registry import (
    SubAgentToolArgsValidationError,
    SubAgentToolDispatcher,
    SubAgentToolDispatchResult,
    SubAgentToolRegistry,
    build_default_subagent_registry,
)
from bob.task_store import Task, TaskStore, TaskStoreError
from bob.ui_registry import ComponentDescriptor, validate_component_descriptor
from bob.validation import (
    SUB_AGENT_DEFAULT_POLICY,
    CallEnvelope,
    ExhaustedContext,
    OnValidationExhausted,
    SubAgentOnValidationExhausted,
    build_validator_message,
    get_policy,
    render_feedback,
)
from bob.validation.reason_codes import (
    REASON_HARD_KILLED,
    REASON_INVALID_OUTPUT,
    REASON_ITERATION_CAP,
    REASON_LLM_FAILED,
    REASON_OK,
    REASON_STALLED,
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


# --- Loop-convergence guards (mail-tool-loop investigation, 2026-05-29) -------
#
# A weak local model (qwen3.5-9b) does not reliably emit ``done``: it keeps
# emitting filler ``progress`` and re-issuing tool calls until a hard cap fires.
# Two distinct stall shapes are bounded here:
#
#   * (original RC1) the model holds a SUCCESSFUL tool result but narrates
#     ``progress`` / re-issues the same ``tool_call`` instead of concluding;
#   * (Trou A/B) the model loops on ``progress`` with NO usable result yet —
#     typically after a tool ERROR, which leaves ``last_tool_result`` None so the
#     original guard never armed. This is the "Dernier mail du jour" hang:
#     ``gmail_search after:"today"`` → invalid_query → 23 ``progress`` lines →
#     external hard-kill with an empty result.
#
# So a "stall" iteration is now ANY non-advancing step: a ``progress``, a
# duplicate ``tool_call``, OR a ``tool_call`` whose dispatch ERRORED. Only a
# fresh SUCCESSFUL tool result (or a ``done``) is forward progress and resets the
# streak. Empirically (logs 2026-05-21..29) no task that ever reached a terminal
# ``done`` emitted more than 3 consecutive ``progress`` — every run with ≥4 was a
# loop — so the force threshold below never truncates a legitimate task.

#: After this many consecutive stall iterations, re-inject a forcing nudge under
#: the ``system_validator`` role. The message is context-aware (see
#: :meth:`SubAgentRunner._stall_nudge_message`): "you already have a result →
#: emit ``done``", "your tool call errored → fix the args or ``done(failed)``",
#: or "stop looping on ``progress``". 2 leaves room for the recipe's single
#: "lecture du mail" reflection between a tool result and the terminal ``done``.
_STALL_NUDGE_THRESHOLD = 2

#: Hard ceiling: after this many consecutive stall iterations the runner stops
#: waiting on the model and force-terminates via
#: :meth:`SubAgentRunner._force_stalled_done` — salvaging the last successful
#: tool result (``done(degraded, stalled)``) or, failing that, naming the last
#: tool error (``done(failed, stalled)``), so the exit is never an empty mystery.
_STALL_FORCE_THRESHOLD = 4

#: Max chars of a salvaged tool-result body folded into a degraded ``done``'s
#: ``result_summary`` so Jarvis' done-synthesis still has the data to answer
#: from without blowing up the synthesis prompt.
_SALVAGE_MAX_CHARS = 2000


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


def _tool_call_key(name: str, args: dict[str, Any]) -> str:
    """Canonical signature for a tool call so an identical repeat is detectable.

    ``sort_keys`` makes the key order-insensitive; ``default=str`` keeps it
    total over the permissive ``args`` bag (which may carry non-JSON-native
    values from a tolerant parse). Used by the runner's dedup guard (RC4):
    a second ``(name, args)`` already seen this run is not re-dispatched.
    """

    try:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical = repr(args)
    return f"{name}\x00{canonical}"


def _find_control_chars(value: Any) -> str | None:
    """Return the first offending substring when ``value`` holds a control char.

    PRD 0008 / mail-tool-loop (2026-05-29, RC5). Under LM Studio guided
    (``response_format: json_schema``) decoding the model mangled a multibyte
    UTF-8 char inside a string arg — ``"intéressement"`` became
    ``"int\x13ressement"`` (``é`` → U+0013, a C0 control char). Such a query
    matches nothing and the model loops. We scan string args (recursing into
    nested dict/list) for C0/C1 control characters other than ``\t\n\r`` and
    surface the offending value so the caller can route a ``system_validator``
    correction (bounded retry) instead of dispatching a corrupted query.
    Returns ``None`` when the value (and everything nested) is clean.
    """

    if isinstance(value, str):
        for ch in value:
            code = ord(ch)
            if (code < 0x20 and ch not in "\t\n\r") or 0x7F <= code <= 0x9F:
                return value
        return None
    if isinstance(value, dict):
        for nested in value.values():
            found = _find_control_chars(nested)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for nested in value:
            found = _find_control_chars(nested)
            if found is not None:
                return found
    return None


def _salvage_tool_result_text(tool_name: str | None, result: dict[str, Any] | None) -> str:
    """Build a degraded-``done`` ``result_summary`` from the last tool result.

    mail-tool-loop (2026-05-29, RC2). When a cap or the stall guard force-
    terminates a run, the data the sub-agent already retrieved (e.g. the very
    email the user asked for) is sitting in the transcript. Rather than emit an
    empty ``done`` — which made Jarvis announce "aucun résultat" despite a
    successful ``gmail_search`` — we fold a compact form of the last successful
    tool result into ``result_summary`` so Jarvis' done-synthesis can still
    answer from it. Deliberately tool-agnostic (no Mail-overlay reconstruction
    here — that lives in the skill pack): the runner stays generic and the data
    survives. Returns ``""`` when there is nothing to salvage.
    """

    if not result:
        return ""
    try:
        body = json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""
    if len(body) > _SALVAGE_MAX_CHARS:
        body = body[:_SALVAGE_MAX_CHARS] + "…"
    label = f"outil {tool_name}" if tool_name else "outil"
    return f"[résultat partiel — limite atteinte] dernier résultat de {label} : {body}"


def _resolve_terminal_deliverable(
    result_store: ToolResultStore, result_ref: str | None = None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Resolve the deterministic deliverable + summary for a terminal exit (PRD 0009/0010).

    Prefers the result the model explicitly referenced via ``result_ref``; falls
    back to the most recent stored result. Returns the projected deliverable as a
    **list of section descriptors** (``list[ComponentDescriptor] | None``; PRD
    0010 / issue 0066 — a single card is a list-of-one, an empty result is
    ``None``) plus the projection's deterministic summary (``None`` when empty).
    This is the SINGLE place the structured deliverable is rebuilt on a forced or
    clean exit, so a card survives a stall / cap / bare ``done`` instead of
    depending on the weak model emitting a perfect ``ui_payload`` (the 2026-05-30
    empty-overlay bug). A ``result_ref`` that does not resolve degrades to
    ``last()`` — the best available guess — rather than dropping the deliverable.
    """

    stored = result_store.get(result_ref) or result_store.last()
    if stored is None:
        return None, None
    projection = stored.projection
    return projection.deliverable, (projection.summary or None)


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


def _is_component_descriptor(
    ui_payload: dict[str, Any] | str | None,
) -> TypeGuard[dict[str, Any]]:
    """True when ``ui_payload`` is a structured ``{component, props}`` descriptor.

    PRD 0008 / issue 0064. A descriptor (e.g. ``{"component": "Mail",
    "props": {...}}`` or ``{"component": "Markdown", "props": {...}}``) must
    survive to the frontend STRUCTURED so the matching overlay can rebuild
    itself — it must NOT be flattened to a markdown string by
    :func:`_deliverable_text`. We key on the presence of a non-empty string
    ``component`` discriminator; the props bag is validated downstream. The
    :class:`typing.TypeGuard` return narrows ``ui_payload`` to ``dict`` for
    the caller so the persisted descriptor stays well-typed.
    """

    return (
        isinstance(ui_payload, dict)
        and isinstance(ui_payload.get("component"), str)
        and bool(ui_payload.get("component"))
    )


def _deliverable_text(ui_payload: dict[str, Any] | str | None) -> str | None:
    """Pull the renderable markdown deliverable out of a ``done`` ui_payload.

    Document-class sub-agents put the finished artefact (exposé, report,
    chronology) in ``ui_payload`` as a markdown string. Structured payloads
    may instead carry it under a ``markdown`` / ``content`` / ``text`` key.
    Returns ``None`` when there is nothing renderable so the caller falls back
    to ``result_summary``.

    PRD 0008 / issue 0064: a structured ``{component, props}`` descriptor is
    NOT flattened here — it carries no top-level markdown key and is meant to
    travel structured to the frontend via ``task.result_payload``. The bug
    this fixes was a Mail descriptor being collapsed to ``None`` (and the
    overlay therefore never rendering). The ``{"component": "Markdown",
    "props": {"content": ...}}`` shape used by the recall path also returns
    ``None`` here on purpose — its renderable text lives under
    ``props.content``, not a top-level key, so the descriptor survives intact.
    """

    if isinstance(ui_payload, str):
        return ui_payload.strip() or None
    if isinstance(ui_payload, dict):
        for key in ("markdown", "content", "text"):
            value = ui_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _model_payload_to_sections(
    model_payload: dict[str, Any] | str | None,
) -> list[dict[str, Any]] | None:
    """Normalise a model-emitted ``done.ui_payload`` into a section list (issue 0066).

    PRD 0010 — the deliverable contract is a **list** of ``{component, props}``
    section descriptors. The sub-agent may still emit its deliverable two legacy
    ways, which we lift onto the new shape here:

    - a structured ``{component, props}`` descriptor (a hand-built Mail / Markdown
      card) → a list-of-one ``[descriptor]``;
    - a document-class markdown string (or a ``{markdown/content/text}`` bag) →
      a single ``Markdown`` section ``[{"component": "Markdown", "props":
      {"content": <text>}}]`` so a plain text deliverable travels through the
      same SectionsOverlay registry as everything else.

    Returns ``None`` when the payload carries nothing renderable, so the caller
    falls through to the store / no-card paths.
    """

    if _is_component_descriptor(model_payload):
        return [model_payload]
    text = _deliverable_text(model_payload)
    if text is not None:
        return [{"component": "Markdown", "props": {"content": text}}]
    return None


def _validate_deliverable(ui_payload: ComponentDescriptor | str | None) -> str | None:
    """Validate a ``done`` deliverable against the ``ui_registry`` schema.

    PRD 0008 / issue 0065. A markdown-string deliverable (or ``None``) is
    always valid — there is no structured contract to satisfy. A structured
    :class:`bob.ui_registry.ComponentDescriptor` has its props validated
    against the SINGLE ``ui_registry`` component schema — the same one the
    ``say`` tool uses, so the deliverable and ``say`` UI can never drift.
    Returns ``None`` when valid, else a single-line error string suitable for
    ``system_validator`` self-correction feedback — mirroring the
    ``tool_call.args`` seam so an invalid deliverable is corrected, never
    silently dropped.
    """

    if not isinstance(ui_payload, ComponentDescriptor):
        return None
    errors = validate_component_descriptor(ui_payload.model_dump())
    return "; ".join(errors) if errors else None


# Issue 0056 — privacy redaction for the Mail ``ui_payload`` in debug events.
#
# The sub-agent's ``done`` action embeds the full Mail props (subject +
# snippet / bodyPreview) so the LLM and the assistant_msg pipeline can carry
# them through to the overlay. That same payload is currently echoed in the
# ``status_change`` :func:`emit_debug` envelope which lands in the in-memory
# ring buffer, on the WS ``/ws/debug`` subscriber feed and on the JSONL file
# sink. Per the PRD's privacy posture only metadata (message id, thread id,
# sender email, label set) may live in DebugEvent payloads — subject /
# snippet / bodyPreview must be scrubbed before emit.
#
# The redaction keys on the component discriminator: a Mail descriptor
# (``{"component": "Mail", "props": {...}}``) gets its props passed through
# the field-level scrubber. Non-Mail payloads (Markdown deliverables,
# arbitrary structured payloads from other tools) are returned unchanged so
# this slice does not regress on the existing Markdown overlay path.
_MAIL_PROP_FIELDS_REDACTED = ("subject", "bodyPreview", "snippet", "body")
_MAIL_REDACTED_PLACEHOLDER = "<redacted-for-privacy>"


def _redact_ui_payload_for_debug(
    ui_payload: dict[str, Any] | str | None,
) -> dict[str, Any] | str | None:
    """Return ``ui_payload`` with email body fields scrubbed for debug events.

    Only Mail descriptors are touched (keyed on ``component == "Mail"``).
    Every other shape is passed through untouched so non-Gmail flows
    (Markdown deliverables, future overlays) are unaffected. The returned
    value is a shallow copy when a Mail dict is detected — never the same
    object — so subsequent mutation of the original payload does not leak
    back into the captured debug event.

    Fields scrubbed: ``subject``, ``bodyPreview``, ``snippet``, ``body``.
    Metadata kept: ``messageId``, ``threadId``, ``from`` (sender), ``flags``,
    ``labels``, ``attachments`` (filename + size + mime — no bytes), the
    ``gmailWebUrl`` deep link, and ``receivedAt``.
    """

    if not isinstance(ui_payload, dict):
        return ui_payload
    if ui_payload.get("component") != "Mail":
        return ui_payload

    redacted_payload = dict(ui_payload)
    raw_props = redacted_payload.get("props")
    if not isinstance(raw_props, dict):
        return redacted_payload
    redacted_props = dict(raw_props)
    for field_name in _MAIL_PROP_FIELDS_REDACTED:
        if field_name in redacted_props:
            redacted_props[field_name] = _MAIL_REDACTED_PLACEHOLDER
    redacted_payload["props"] = redacted_props
    return redacted_payload


def _validate_sections(
    sections: list[dict[str, Any]] | None, *, task_id: str
) -> list[dict[str, Any]]:
    """Validate each section descriptor against the ui_registry schema (issue 0066).

    PRD 0010 robustness invariant. The deterministic paths (convergence /
    forced stall / cap) build sections from a tool projector and bypass the
    model-path validator (issue 0065), so this is their safety net. Each
    descriptor's props are validated against the SINGLE ui_registry schema; a
    section that fails is DROPPED (per-section, never crashing the whole list)
    and logged. Idempotent for the already-validated model path. Returns the
    surviving sections (possibly empty); the caller collapses an empty list to
    ``None``.
    """

    if not sections:
        return []
    kept: list[dict[str, Any]] = []
    for section in sections:
        errors = validate_component_descriptor(section)
        if errors:
            _logger.warning(
                "sub_agent_runner.invalid_section_dropped",
                task_id=task_id,
                component=section.get("component") if isinstance(section, dict) else None,
                errors=errors,
            )
            continue
        kept.append(section)
    return kept


def _sections_markdown_text(sections: list[dict[str, Any]] | None) -> str | None:
    """Markdown text of the FIRST renderable Markdown section, else ``None`` (issue 0066).

    Drives the ``task.result`` fallback text: a document-class deliverable now
    travels as a single ``Markdown`` section, so the renderable string lives in
    ``props.content`` (or a ``markdown`` / ``text`` key). A Mail-only list yields
    ``None`` — its content lives in ``result_payload`` and the spoken
    ``result_summary`` becomes the ``task.result`` text instead.
    """

    if not sections:
        return None
    for section in sections:
        if not isinstance(section, dict):
            continue
        if section.get("component") != "Markdown":
            continue
        text = _deliverable_text(section.get("props"))
        if text is not None:
            return text
    return None


def _redact_sections_for_debug(
    sections: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Per-section email-field redaction for debug sinks (issue 0056 + 0066).

    Maps :func:`_redact_ui_payload_for_debug` over each section so a Mail
    section's subject / body is scrubbed before the list lands in the debug ring
    buffer / ``/ws/debug`` feed / JSONL sink. Non-Mail sections pass through
    untouched. ``None`` in → ``None`` out.
    """

    if sections is None:
        return None
    redacted: list[dict[str, Any]] = []
    for section in sections:
        scrubbed = _redact_ui_payload_for_debug(section)
        # Sections are always ``{component, props}`` dicts (validated upstream),
        # so the redactor returns a dict here; the ``str | None`` arm of its
        # signature only fires for the legacy raw-string ``ui_payload`` caller.
        if isinstance(scrubbed, dict):
            redacted.append(scrubbed)
    return redacted


def _sections_contain_mail(sections: list[dict[str, Any]] | None) -> bool:
    """True when any section is a ``Mail`` descriptor (drives debug ``result`` elision)."""

    if not sections:
        return False
    return any(
        isinstance(section, dict) and section.get("component") == "Mail" for section in sections
    )


def _salvage_display(raw: str) -> str:
    """Best-effort clean text from a sub-agent output that failed validation.

    The model frequently emits the JSON envelope it *meant* to send but with a
    shape the schema rejects. Rather than surfacing the raw ``{"action": …}``
    blob in the overlay, decode it and extract the deliverable (``ui_payload``)
    or summary. Falls back to the stripped raw text when it is not JSON at all.
    """

    text = _strip_code_fence(raw).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return raw.strip()
    if isinstance(payload, dict):
        deliverable = _deliverable_text(payload.get("ui_payload"))
        if deliverable:
            return deliverable
        summary = payload.get("result_summary") or payload.get("result")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return raw.strip()


@dataclass(frozen=True)
class _NormalisedPayload:
    """Internal: an LLM action payload normalised against the v1 schema.

    Carries either a :class:`SubAgentAction` variant (parsed via the
    versioned schema) or a synthetic ``ask_user`` payload preserved for
    the legacy flow. Exactly one of the fields is populated.
    """

    action: ProgressAction | ToolCallAction | DoneAction | None = None
    ask_user_question: str | None = None


@dataclass(frozen=True)
class _ToolCallStep:
    """Outcome of :meth:`SubAgentRunner._handle_tool_call`.

    Exactly one of the two fields is populated:

    - ``validation_error`` — a PRE-dispatch model mistake (unknown tool /
      schema-invalid args / control-char args). The call was NOT dispatched;
      the caller routes a ``system_validator`` correction bounded by the
      per-tool retry policy (issue 0062). Mirrors the legacy non-``None``
      return that the loop already handled.
    - ``dispatched`` — the tool actually ran; this carries its
      :class:`SubAgentToolDispatchResult` (success OR a genuine runtime tool
      error), already persisted as a ``tool`` message. The loop reads it to
      track the last successful result for salvage (RC2) and to record the
      call signature for dedup (RC4).
    - ``stored`` — PRD 0009: on a SUCCESSFUL dispatch, the
      :class:`StoredResult` written to the run's blackboard (ref + projection).
      ``None`` on a validation error or a runtime tool error (only successes
      are stored). The loop carries it so a terminal exit can build the
      deliverable from the projection deterministically (P4).
    """

    validation_error: SubAgentToolDispatchResult | None = None
    dispatched: SubAgentToolDispatchResult | None = None
    stored: StoredResult | None = None


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


def _render_tool_catalogue(registry: SubAgentToolRegistry) -> str:
    """Render the per-tool argument JSON Schema block for the system prompt.

    Issue 0059 (PRD 0008). Replaces the former name+description-only listing
    (``- `name` : description``) — which forced the model to *guess* argument
    names from the prose recipe — with each tool's real argument JSON Schema,
    derived from its Pydantic ``args_model`` via
    :meth:`SubAgentToolDefinition.to_spec`. The description is kept as a one-
    line header (it carries the operational "when to use this" guidance the
    model still needs); the schema is appended verbatim so the model can read
    the exact field names, types and which are required.

    The schema is serialised with ``sort_keys=True`` and a fixed indent so the
    block is deterministic (stable across runs / Python dict-ordering) — the
    0057 golden-prompt posture wants byte-stable prompts. Tools are emitted in
    name-sorted order (issue 0063) so the prompt prefix stays byte-stable for
    cache hits regardless of registration order. Returns the empty string for
    an empty registry so the caller can skip the section header.
    """

    blocks: list[str] = []
    for definition in sorted(registry, key=lambda d: d.name):
        spec = definition.to_spec()
        schema_json = json.dumps(spec.parameters, ensure_ascii=False, sort_keys=True, indent=2)
        blocks.append(
            f"- ``{spec.name}`` : {spec.description}\n"
            f"  Arguments (JSON Schema) :\n```json\n{schema_json}\n```"
        )
    return "\n".join(blocks)


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
        # PRD 0008 / issue 0060 — on a backend that token-gates guided JSON
        # (LM Studio's ``response_format``) we constrain the control envelope
        # to the ``SubAgentAction`` schema so a fenced / prose-wrapped /
        # ``json.loads``-failing envelope is impossible by construction. The
        # schema is derived ONCE from the union (single source). Non-guided
        # backends (Claude CLI) keep ``None`` and stay on the tolerant
        # ``_normalise_payload`` parse path unchanged.
        self._envelope_schema: dict[str, Any] | None = (
            sub_agent_action_response_schema() if subagent_client.supports_guided_json() else None
        )
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

        # --- Loop-convergence state (mail-tool-loop, 2026-05-29) -------------
        # ``seen_tool_calls`` maps a canonical ``(name, args)`` signature to its
        # repeat count so an identical call is rejected instead of re-dispatched
        # (RC4). ``last_tool_result`` / ``last_tool_name`` hold the most recent
        # SUCCESSFUL tool result so a cap / stall exit can salvage it into the
        # terminal done instead of discarding it (RC2). ``last_tool_error`` holds
        # the most recent FAILED dispatch (Trou B) so a stall exit can name the
        # error instead of failing silently. ``stall_count`` is the run of
        # non-advancing iterations — a ``progress``, a duplicate ``tool_call``, or
        # an ERRORED ``tool_call`` (Trou A/B) — driving the forcing function
        # (RC1); it resets to 0 ONLY on a new successful tool result.
        seen_tool_calls: dict[str, int] = {}
        last_tool_result: dict[str, Any] | None = None
        last_tool_name: str | None = None
        last_tool_error: SubAgentToolDispatchResult | None = None
        stall_count = 0

        # PRD 0009 — per-run blackboard. Each successful tool dispatch writes its
        # full result here keyed by a short ref; the transcript carries only the
        # projected compact digest (context saving), and a terminal exit builds
        # the deliverable from the stored projection deterministically (P4) — so
        # the Mail card no longer depends on the weak model emitting a perfect
        # ``done`` (2026-05-30).
        result_store = ToolResultStore()

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
                    # RC2 — surface whatever the agent already retrieved rather
                    # than an empty done (which read as "aucun résultat").
                    result_summary=_salvage_tool_result_text(last_tool_name, last_tool_result),
                    cost=self._build_cost(
                        started_at=started_at,
                        iterations=iteration,
                        tokens_used=tokens_used,
                    ),
                    # PRD 0009 — also rebuild the structured deliverable (the
                    # Mail card) from the store so the overlay is not empty.
                    result_store=result_store,
                    redact_result_in_debug=True,
                )
                return

            elapsed = self._clock() - started_at
            if elapsed >= self._policy.wall_clock_seconds:
                # NOTE (PRD 0009): unlike the iteration / token caps (status
                # ``degraded`` → ``done`` → persists ``result_payload``), a
                # wall-clock timeout is ``status="timeout"`` → ``failed``, and the
                # failed path deliberately does not persist ``result_payload``
                # (legacy ``_fail`` semantics, asserted by tests). So we do NOT
                # thread the store here: it would attach a card to the live WS
                # frame but not the DB, an inconsistency worse than the status
                # quo. Treating a timeout-with-data as ``degraded`` (so the card
                # persists) is a deliberate semantic change left as a follow-up.
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
                    # RC2 — salvage the retrieved data instead of an empty done.
                    result_summary=_salvage_tool_result_text(last_tool_name, last_tool_result),
                    cost=self._build_cost(
                        started_at=started_at,
                        iterations=iteration,
                        tokens_used=tokens_used,
                    ),
                    # PRD 0009 — rebuild the structured deliverable from the store.
                    result_store=result_store,
                    redact_result_in_debug=True,
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
                # Issue 0060 — pass the derived envelope schema ONLY on a
                # guided backend (``self._envelope_schema`` is ``None``
                # otherwise). On LM Studio this becomes a ``response_format``
                # json_schema so the reply is clean ``{"action": …}`` JSON and
                # the ``_normalise_payload`` fence/prose tolerance below is
                # never the failure mode; on Claude CLI ``schema`` stays unset
                # and the path is byte-for-byte unchanged.
                raw = await self._client.chat(
                    messages,
                    schema=self._envelope_schema,
                    session_id=task_id,
                )
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
                    # Retry budget exhausted. The sub-agent never produced a
                    # parseable action envelope — but for deliverable tasks the
                    # model frequently just emits the finished content as raw
                    # prose/markdown (claude -p does this for big outputs, e.g.
                    # a full chronology) and nagging it to re-wrap as JSON only
                    # burns another call. Rather than discarding minutes of work
                    # as done(failed, invalid_output), salvage the raw output as
                    # a degraded done so it survives to Jarvis' done-synthesis
                    # for interpretation + display. Truly empty output keeps the
                    # original forced-failure path (nothing to salvage).
                    salvaged = _salvage_display(raw)
                    emit_debug(
                        category="task",
                        severity="warn",
                        source="bob.sub_agent_runner._run",
                        summary=(
                            "Sortie sub-agent non conforme après retries — "
                            f"{'salvage en done(degraded)' if salvaged else 'échec (vide)'}"
                        ),
                        payload={
                            "task_id": task_id,
                            "last_error": str(exc),
                            "raw_preview": salvaged[:300],
                            "attempts": validator_envelope.attempts,
                        },
                    )
                    if salvaged:
                        await self._finalize_done(
                            task_id,
                            status="degraded",
                            reason_code=REASON_INVALID_OUTPUT,
                            result_summary=salvaged,
                            sections=None,
                            cost=self._build_cost(
                                started_at=started_at,
                                iterations=iteration,
                                tokens_used=tokens_used,
                            ),
                        )
                    else:
                        await self._on_validation_exhausted.on_validation_exhausted(
                            ExhaustedContext(
                                envelope=validator_envelope,
                                last_error_message=str(exc),
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

            # NOTE on the validator budget reset (issue 0048 + 0062): the
            # buffer is dropped on a successful *outcome* (valid progress, a
            # dispatched tool call, or a terminal done), NOT merely on a
            # successful parse. A tool_call whose ARGS fail validation (0062)
            # parses fine but is not a usable outcome — keeping the budget
            # alive across those iterations is what bounds the arg-retry loop
            # by the per-tool RetryPolicy below. Resetting here (on parse)
            # would refresh the budget every iteration and the bound would
            # never bite.

            if normalised.ask_user_question is not None:
                # Legacy ask_user path — preserved until 0050 replaces it
                # with the v2 addendum flow.
                await self._handle_ask_user(task_id, normalised.ask_user_question)
                return

            assert normalised.action is not None  # mypy
            action = normalised.action

            if isinstance(action, DoneAction):
                # Issue 0065 — validate the deliverable (the envelope's OUTPUT
                # half) before finalising. A structured ComponentDescriptor
                # whose props violate the single ui_registry schema is the
                # model's mistake, not a result: route the correction under the
                # ``system_validator`` role (NEVER ``tool`` — prompt-injection
                # safety, PRD 0006), bounded by the same envelope retry budget
                # as a parse failure. On exhaustion the shared handler forces a
                # terminal done(failed, invalid_output) — never a silent drop.
                deliverable_error = _validate_deliverable(action.ui_payload)
                if deliverable_error is not None:
                    policy = SUB_AGENT_DEFAULT_POLICY
                    if validator_envelope.retries_used >= policy.max_retries:
                        await self._on_validation_exhausted.on_validation_exhausted(
                            ExhaustedContext(
                                envelope=validator_envelope,
                                last_error_message=deliverable_error,
                                task_id=task_id,
                            )
                        )
                        return
                    validator_feedback.append(
                        build_validator_message(
                            render_feedback(
                                error_message=(
                                    "Ton livrable ``ui_payload`` est invalide "
                                    f"({deliverable_error}). Ré-essaye en émettant "
                                    "un objet {component, props} conforme au schéma "
                                    "du composant, ou une chaîne Markdown."
                                ),
                                offending_raw=json.dumps(
                                    action.ui_payload.model_dump()
                                    if isinstance(action.ui_payload, ComponentDescriptor)
                                    else action.ui_payload,
                                    ensure_ascii=False,
                                ),
                            )
                        )
                    )
                    validator_envelope.record_feedback(validator_feedback[-1]["content"])
                    validator_envelope.increment(error_code=REASON_INVALID_OUTPUT)
                    continue
                await self._handle_done(task_id, action, result_store)
                return

            if isinstance(action, ProgressAction):
                iteration += 1
                # Valid-ish step — clear any pending validator state up front;
                # the stall guard below may re-arm a nudge for the next call.
                validator_feedback = []
                validator_envelope = CallEnvelope(tool_name=None, actor="sub_agent")
                await self._handle_progress(task_id, action.thought)
                # RC1 + Trou A (mail-tool-loop) — consecutive ``progress`` is a
                # stall whether or not a tool result exists yet. A weak model
                # narrates "j'ai appelé X" without ever emitting ``done`` — and
                # after a tool ERROR (``last_tool_result`` stays None) the original
                # RC1 guard never armed, so the run spun 23 progress lines until an
                # external hard-kill. Count EVERY progress; only a fresh successful
                # tool result or a ``done`` resets the streak.
                stall_count += 1
                decision = self._stall_decision(stall_count)
                if decision == "force":
                    await self._force_stalled_done(
                        task_id,
                        last_tool_result=last_tool_result,
                        last_tool_name=last_tool_name,
                        last_tool_error=last_tool_error,
                        result_store=result_store,
                        started_at=started_at,
                        iteration=iteration,
                        tokens_used=tokens_used,
                    )
                    return
                if decision == "nudge":
                    validator_feedback = [
                        build_validator_message(
                            render_feedback(
                                error_message=self._stall_nudge_message(
                                    last_tool_result=last_tool_result,
                                    last_tool_error=last_tool_error,
                                ),
                                offending_raw=None,
                            )
                        )
                    ]
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
                # RC4 (mail-tool-loop) — an identical ``(name, args)`` call
                # already issued this run is NOT re-dispatched: its result is
                # already in the transcript. Suppress it (no dispatch, no
                # transcript bloat), nudge the model to use the result or emit
                # ``done``, and count the repeat toward the stall guard so a
                # model that ignores the nudge still converges.
                call_key = _tool_call_key(action.name, action.args)
                if call_key in seen_tool_calls:
                    seen_tool_calls[call_key] += 1
                    stall_count += 1
                    emit_debug(
                        category="task",
                        severity="debug",
                        source="bob.sub_agent_runner._handle_tool_call",
                        summary=f"Appel outil {action.name} ignoré (doublon)",
                        payload={
                            "task_id": task_id,
                            "tool": action.name,
                            "args": action.args,
                            "kind": "tool_dedup",
                            "repeat_count": seen_tool_calls[call_key],
                        },
                    )
                    decision = self._stall_decision(stall_count)
                    if decision == "force":
                        await self._force_stalled_done(
                            task_id,
                            last_tool_result=last_tool_result,
                            last_tool_name=last_tool_name,
                            last_tool_error=last_tool_error,
                            result_store=result_store,
                            started_at=started_at,
                            iteration=iteration,
                            tokens_used=tokens_used,
                        )
                        return
                    if decision == "nudge":
                        validator_feedback = [
                            build_validator_message(
                                render_feedback(
                                    error_message=(
                                        f"Tu as déjà appelé ``{action.name}`` avec ces "
                                        "mêmes arguments — le résultat est déjà dans le "
                                        "contexte ci-dessus. N'appelle PAS le même outil à "
                                        "nouveau : émets ``done`` avec le livrable construit "
                                        "à partir de ce résultat, ou change d'arguments si "
                                        "tu cherches vraiment autre chose."
                                    ),
                                    offending_raw=None,
                                )
                            )
                        ]
                    continue

                step = await self._handle_tool_call(task_id, action, result_store)
                validation_error = step.validation_error
                if validation_error is not None:
                    # Issue 0062 — a pre-dispatch arg-validation / unknown-tool
                    # failure is the model's mistake, not a tool result. Route
                    # the correction under the ``system_validator`` role (NEVER
                    # ``tool`` — prompt-injection safety, PRD 0006), bounded by
                    # the per-tool RetryPolicy. On exhaustion the shared
                    # ``on_validation_exhausted`` handler forces a terminal
                    # done(failed, invalid_output) — explicit, never a silent
                    # drop and never an unbounded tool round-trip.
                    policy = get_policy(action.name)
                    if validator_envelope.retries_used >= policy.max_retries:
                        await self._on_validation_exhausted.on_validation_exhausted(
                            ExhaustedContext(
                                envelope=validator_envelope,
                                last_error_message=(
                                    validation_error.error_message or "invalid tool arguments"
                                ),
                                task_id=task_id,
                            )
                        )
                        return
                    validator_feedback.append(
                        build_validator_message(
                            render_feedback(
                                error_message=(
                                    f"Ton appel à l'outil ``{action.name}`` est invalide "
                                    f"({validation_error.error_code}): "
                                    f"{validation_error.error_message}. Ré-essaye en émettant "
                                    "des arguments conformes au schéma JSON de l'outil."
                                ),
                                offending_raw=json.dumps(
                                    {"name": action.name, "args": action.args},
                                    ensure_ascii=False,
                                ),
                            )
                        )
                    )
                    validator_envelope.record_feedback(validator_feedback[-1]["content"])
                    validator_envelope.tool_name = action.name
                    validator_envelope.increment(error_code=validation_error.error_code)
                    continue
                # Valid tool call dispatched — record its signature for dedup
                # (RC4). A SUCCESSFUL result is genuine forward progress: keep it
                # for salvage (RC2), clear any stale error, reset the stall streak.
                seen_tool_calls[call_key] = seen_tool_calls.get(call_key, 0) + 1
                if step.dispatched is not None and step.dispatched.ok:
                    last_tool_result = step.dispatched.result
                    last_tool_name = step.dispatched.tool_name
                    last_tool_error = None
                    stall_count = 0
                    validator_feedback = []
                    validator_envelope = CallEnvelope(tool_name=None, actor="sub_agent")
                    # PRD 0009 P5 — CONVERGE. A terminal projection (a single-shot
                    # answer like a mail lookup, ``project_gmail_search`` marks
                    # ``terminal=True``) lets the runner finalise ``done`` NOW,
                    # deterministically, from the store — instead of waiting for a
                    # weak model to emit ``done`` (which it routinely fails to do:
                    # it spins filler ``progress`` until the stall guard fires,
                    # 2026-05-30 RC1). This removes the stall window on the happy
                    # path entirely and saves ~3 wasted LLM calls. The deliverable
                    # + spoken summary come from the projection; privacy redaction
                    # of the Mail descriptor in debug sinks is handled by
                    # ``_finalize_done`` as on any other done. Multi-step tools mark
                    # their projection non-terminal and never converge here; the
                    # flag also lets an operator disable it wholesale.
                    if (
                        self._policy.converge_on_terminal_result
                        and step.stored is not None
                        and step.stored.projection.terminal
                    ):
                        projection = step.stored.projection
                        await self._finalize_done(
                            task_id,
                            status="complete",
                            reason_code=REASON_OK,
                            result_summary=projection.summary,
                            sections=projection.deliverable,
                            cost=self._build_cost(
                                started_at=started_at,
                                iterations=iteration,
                                tokens_used=tokens_used,
                            ),
                        )
                        return
                    continue
                if step.dispatched is not None and not step.dispatched.ok:
                    # Trou B (mail-tool-loop) — the handler RAN and FAILED (e.g.
                    # gmail_search ``after:"today"`` → invalid_query). The error is
                    # already in the transcript as a ``tool`` message, so below the
                    # nudge threshold we let the model read it and self-correct.
                    # But a weak model loops narrating ``progress`` instead of
                    # retrying — so an errored dispatch is NOT forward progress: it
                    # counts toward the stall guard, nudges with the error detail
                    # past the threshold, then force-terminates. Without this a tool
                    # that only ever errors leaves ``last_tool_result`` None and the
                    # loop is unbounded until a hard cap / external kill.
                    last_tool_error = step.dispatched
                    stall_count += 1
                    decision = self._stall_decision(stall_count)
                    if decision == "force":
                        await self._force_stalled_done(
                            task_id,
                            last_tool_result=last_tool_result,
                            last_tool_name=last_tool_name,
                            last_tool_error=last_tool_error,
                            result_store=result_store,
                            started_at=started_at,
                            iteration=iteration,
                            tokens_used=tokens_used,
                        )
                        return
                    if decision == "nudge":
                        validator_feedback = [
                            build_validator_message(
                                render_feedback(
                                    error_message=self._stall_nudge_message(
                                        last_tool_result=last_tool_result,
                                        last_tool_error=last_tool_error,
                                    ),
                                    offending_raw=None,
                                )
                            )
                        ]
                    else:
                        validator_feedback = []
                    validator_envelope = CallEnvelope(tool_name=None, actor="sub_agent")
                    continue
                # Defensive — dispatch produced no result object (the persist-
                # failure path in ``_handle_tool_call`` returns ``_ToolCallStep()``
                # with ``dispatched=None``). Clear validator state and loop.
                validator_feedback = []
                validator_envelope = CallEnvelope(tool_name=None, actor="sub_agent")
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

        tool_catalogue = _render_tool_catalogue(self._tool_registry)
        system_prompt = SUB_AGENT_V2_SYSTEM_PROMPT.render(goal=task.goal)
        # Issue 0063: append any goal-matching skill packs (the Gmail recipe
        # today) so the base contract stays tool-agnostic and a non-matching
        # goal never pays for an irrelevant recipe's tokens.
        for pack in select_skill_packs(task.goal):
            system_prompt += "\n\n" + pack.render()
        if tool_catalogue:
            system_prompt += "\n\nOutils disponibles :\n" + tool_catalogue

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

    async def _handle_done(
        self, task_id: str, action: DoneAction, result_store: ToolResultStore
    ) -> None:
        """Persist a ``done`` action coming from the LLM.

        PRD 0009 — the terminal deliverable is, by preference, a deterministic
        projection of a stored tool result (resolved by ``action.result_ref``
        or, for a bare ``done`` emitted with a result in hand, the last stored
        result), so a Mail card no longer depends on the weak model reproducing
        it. A model-provided ``ui_payload`` (a document-class markdown string or
        a hand-built descriptor) is still honoured when there is no usable stored
        result to prefer (see :meth:`_select_done_deliverable`).
        """

        # Issue 0065 — ``ui_payload`` is the typed Deliverable union. Normalise
        # a validated ComponentDescriptor back to its plain dict form here so
        # the persistence + transport path (0064) and the debug / redaction
        # helpers keep operating on ``dict | str | None`` exactly as before.
        deliverable = action.ui_payload
        model_payload: dict[str, Any] | str | None
        if isinstance(deliverable, ComponentDescriptor):
            model_payload = deliverable.model_dump()
        else:
            model_payload = deliverable

        sections, result_summary = self._select_done_deliverable(
            result_store,
            result_ref=action.result_ref,
            model_payload=model_payload,
            result_summary=action.result_summary,
        )
        await self._finalize_done(
            task_id,
            status=action.status,
            reason_code=action.reason_code,
            result_summary=result_summary,
            sections=sections,
            cost=action.cost,
        )

    @staticmethod
    def _select_done_deliverable(
        result_store: ToolResultStore,
        *,
        result_ref: str | None,
        model_payload: dict[str, Any] | str | None,
        result_summary: str,
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """Pick the ``done`` deliverable sections + summary (PRD 0009 / 0010).

        Returns a **list of section descriptors** (``list[ComponentDescriptor] |
        None`` — issue 0066; a single card is a list-of-one) plus the summary.

        Precedence:

        (a) the model referenced a stored result (``result_ref``) whose
            projection has a deliverable → build the sections from it (the
            "pass the data id" path — the model never copies the payload);
        (b) the model emitted its OWN renderable payload — a document-class
            markdown string or a hand-built ``{component, props}`` descriptor →
            respect it, normalised to a list-of-one section (covers document
            tasks and a model that did build a card);
        (c) no usable model payload, but a tool result with a deliverable is on
            the blackboard → build the sections from the last stored result (the
            2026-05-30 RC1 case: the model finally emits a *bare* ``done``).
            Skipped when an explicit ``result_ref`` already RESOLVED — see below;
        (d) nothing structured anywhere → ``None`` (the ``result`` text remains
            the rendering source), unchanged from pre-0010 behaviour.

        A RESOLVED ``result_ref`` is authoritative: the model chose THAT result,
        so we never substitute a different (e.g. a later, different-tool) card
        for it. If the referenced result has a deliverable we use it (a); if it
        resolved but has none — an empty search — we respect "no card" and fall
        to the model's own payload (b) only, NOT to ``last()`` (c). A ref that
        does NOT resolve (a typo) is ignored and we proceed as if none was given.

        In (a)/(c) a non-empty model ``result_summary`` wins over the projection
        summary (the model may have written better prose); otherwise the
        deterministic projection summary fills it in.
        """

        ref_resolved = False
        if result_ref:
            stored = result_store.get(result_ref)
            if stored is not None:
                ref_resolved = True
                if stored.projection.deliverable is not None:
                    return stored.projection.deliverable, (
                        result_summary or stored.projection.summary
                    )
        model_sections = _model_payload_to_sections(model_payload)
        if model_sections is not None:
            return model_sections, result_summary
        if not ref_resolved:
            stored = result_store.last()
            if stored is not None and stored.projection.deliverable is not None:
                return stored.projection.deliverable, (result_summary or stored.projection.summary)
        return None, result_summary

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

    def _validate_tool_args(self, action: ToolCallAction) -> SubAgentToolDispatchResult | None:
        """Validate ``action.args`` against the tool's ``args_model`` pre-dispatch.

        Issue 0059 (PRD 0008). The sub-agent model used to guess argument
        names from prose; now the prompt advertises each tool's real argument
        JSON Schema (see :func:`_render_tool_catalogue`) and we validate the
        emitted ``args`` against the *same* Pydantic ``args_model`` BEFORE
        dispatching. This closes the "blind dispatch / silent drop" gap: an
        unknown tool name or a payload that violates the schema produces a
        STRUCTURED error result here instead of reaching the handler with
        garbage.

        Returns ``None`` when the args are valid (the caller dispatches
        normally). Returns a populated :class:`SubAgentToolDispatchResult`
        (``error/unknown_tool`` or ``error/invalid_args``) when they are not.
        Schema failures are funnelled through a raised
        :class:`SubAgentToolArgsValidationError` (caught here). Issue 0062 then
        routes that structured error under the ``system_validator`` role for a
        bounded retry (see :meth:`_run`) — NEVER as a ``tool`` message, which
        would echo the model's own bad output back under a trusted role.
        """

        definition = self._tool_registry.get(action.name)
        if definition is None:
            return SubAgentToolDispatchResult(
                outcome="error",
                tool_name=action.name,
                tool_version=None,
                error_code="unknown_tool",
                error_message=f"unknown sub-agent tool: {action.name}",
            )

        # RC5 (mail-tool-loop) — reject control chars in string args BEFORE
        # pydantic (which accepts them as valid ``str``). Guided decoding can
        # mangle a multibyte UTF-8 char (``é`` → U+0013) into a query that
        # matches nothing and feeds the loop; route a bounded ``system_validator``
        # correction instead of dispatching the corrupted call.
        offending = _find_control_chars(action.args)
        if offending is not None:
            return SubAgentToolDispatchResult(
                outcome="error",
                tool_name=definition.name,
                tool_version=definition.version,
                error_code="invalid_args",
                error_message=(
                    "argument string contains a control character (likely a "
                    "mangled accented character from constrained decoding) — "
                    "re-emit the value cleanly in UTF-8"
                ),
            )

        try:
            try:
                definition.args_model.model_validate(action.args)
            except ValidationError as exc:
                raise SubAgentToolArgsValidationError(
                    tool_name=definition.name,
                    message=str(exc),
                ) from exc
        except SubAgentToolArgsValidationError as exc:
            return SubAgentToolDispatchResult(
                outcome="error",
                tool_name=exc.tool_name,
                tool_version=definition.version,
                error_code="invalid_args",
                error_message=exc.message,
            )
        return None

    async def _handle_tool_call(
        self, task_id: str, action: ToolCallAction, result_store: ToolResultStore
    ) -> _ToolCallStep:
        """Execute a sub-agent-side tool call and persist its outcome.

        Returns a :class:`_ToolCallStep`: ``dispatched`` carries the
        :class:`SubAgentToolDispatchResult` of a call that actually ran (success
        or a genuine runtime tool error, persisted as a ``tool`` message and
        round-tripping into the next iteration's prompt); ``validation_error``
        carries a PRE-dispatch model mistake instead.

        Issue 0059 validated ``action.args`` against the tool's ``args_model``
        BEFORE dispatch. Issue 0062 changes what happens on a validation
        failure: instead of round-tripping the structured error as a ``tool``
        message — feeding the model's own malformed output back under a role
        it is trained to trust (a prompt-injection hazard, PRD 0006) — we
        RETURN it under ``_ToolCallStep.validation_error`` to :meth:`_run`,
        which injects the correction under the ``system_validator`` role bounded
        by the per-tool :class:`RetryPolicy`. A *runtime* tool error (the handler
        ran and failed) is a legitimate tool result and stays on ``tool``.
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
            return _ToolCallStep()

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

        # Issue 0059 — pre-dispatch schema validation. Issue 0062 — a failure
        # here is the model's mistake, not a tool result: return it so the
        # caller routes the correction under ``system_validator`` (bounded
        # retry). No blind dispatch, no silent drop, and crucially no ``tool``
        # message echoing the bad output back under a trusted role.
        validation_error = self._validate_tool_args(action)
        if validation_error is not None:
            _logger.warning(
                "sub_agent_runner.tool_args_invalid",
                task_id=task_id,
                tool=action.name,
                error_code=validation_error.error_code,
                error_message=validation_error.error_message,
            )
            return _ToolCallStep(validation_error=validation_error)

        result = await self._tool_dispatcher.dispatch(
            name=action.name,
            arguments=action.args,
            context=_RuntimeToolContext(task_id=task_id),
        )

        # PRD 0009 — a SUCCESSFUL result is written to the per-run blackboard and
        # the ``tool`` transcript message carries only its COMPACT projected
        # digest + the ref, never the full blob. This is the context-saving lever
        # (D2): a weak model no longer re-reads a 2 KB Gmail result every turn,
        # and the body (0056) never enters the transcript. The full result lives
        # in the store; the deliverable is rebuilt from it deterministically at a
        # terminal exit (P4). An un-projected tool's digest IS its full result,
        # so its transcript message is byte-identical to pre-0009. An ERROR is
        # not stored (nothing to project) and keeps the structured error body so
        # the model can read it and self-correct.
        stored: StoredResult | None = None
        body: dict[str, Any]
        if result.ok:
            definition = self._tool_registry.get(action.name)
            stored = result_store.put(
                tool_name=result.tool_name,
                tool_version=result.tool_version,
                result=result.result,
                projector=definition.result_projector if definition else None,
            )
            body = {
                "status": "ok",
                "result_ref": stored.ref,
                "result": stored.projection.digest,
            }
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
            return _ToolCallStep(dispatched=result, stored=stored)

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
        return _ToolCallStep(dispatched=result, stored=stored)

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
            sections=None,
            cost={},
        )

    async def _finalize_done(
        self,
        task_id: str,
        *,
        status: SubAgentDoneStatus,
        reason_code: str,
        result_summary: str,
        sections: list[dict[str, Any]] | None,
        cost: dict[str, Any],
        redact_result_in_debug: bool = False,
        persist_result_on_failure: bool = False,
    ) -> None:
        """Persist the terminal state + emit WS / bus events.

        PRD 0010 / issue 0066 — the structured deliverable is a **list** of
        ``{component, props}`` section descriptors (``sections``). A single card
        is a list-of-one; ``None`` (or an empty list) means "no structured
        deliverable" so the ``result`` text remains the rendering source. The
        list is persisted as ``task.result_payload`` (a JSON array) and shipped
        on the ``task_result`` WS event so the frontend ``SectionsOverlay``
        rebuilds itself.

        ``status in {complete, degraded}`` → task row state ``done`` with
        ``result_summary`` recorded as ``task.result``.
        ``status in {failed, cancelled, timeout}`` → task row state
        ``failed`` with the ``result_summary`` (or the reason code if
        empty) recorded as both a ``system`` message and ``task.result``
        so the existing ``task_result`` WS event still surfaces a string.

        ``persist_result_on_failure`` (mail-tool-loop, 2026-05-29, Trou B):
        a ``failed`` row normally leaves ``task.result`` ``None`` (legacy
        ``_fail`` semantics). But the orchestrator's *failed*-synthesis reads
        ONLY ``task.result`` (``orchestrator._do_generate_failed_synthesis``),
        so a forced ``done(failed, stalled)`` whose ``result_summary`` names
        the tool error would otherwise reach Jarvis as an empty "Raison brute".
        With this flag set (the stalled-tool-error path only) the non-empty
        ``result_summary`` is ALSO written to ``task.result`` via
        :meth:`TaskStore.set_result`, so Jarvis can explain *why* it failed.
        Every other failure path leaves the flag False and is byte-identical.

        ``redact_result_in_debug`` (mail-tool-loop, 2026-05-29): when the
        ``result_summary`` is a SALVAGED tool result (RC2) it may embed raw tool
        output — e.g. a Gmail ``bodyPreview`` — which the issue 0056 privacy
        posture forbids in the debug ring buffer / ``/ws/debug`` feed / JSONL
        sink. With this flag the real text still flows to the chat client via
        ``task.result`` + the ``task_result`` WS frame, but every DEBUG mirror
        of it (the ``status_change`` envelope's ``result`` and the bus-captured
        ``task_result`` copy) is elided. Defaults False so normal dones are
        unchanged.

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
        # PRD 0010 / issue 0066 — the deliverable is a LIST of section
        # descriptors carried to the frontend STRUCTURED via
        # ``task.result_payload`` (a JSON array) + the ``task_result`` WS event
        # so the ``SectionsOverlay`` rebuilds itself. Each section is validated
        # against the single ui_registry schema; a buggy / malformed section is
        # DROPPED here (per-section, never crashing the whole list) rather than
        # shipped. The DETERMINISTIC paths (convergence / forced stall / cap)
        # build sections from a tool projector and call ``_finalize_done``
        # directly, bypassing the model-path validator (issue 0065) — so this
        # re-validation is the safety net for them. Idempotent for the model
        # path (already validated by the same validator), so no valid section is
        # ever dropped. An all-dropped / empty list collapses to ``None``.
        validated_sections = _validate_sections(sections, task_id=task_id)
        structured_payload: list[dict[str, Any]] | None = validated_sections or None
        if status in ("complete", "degraded"):
            store_state = "done"
            # The overlay renders ``task.result`` as markdown when no structured
            # section is present. Prefer the markdown text of a single Markdown
            # section (the exposé / report the sub-agent produced); fall back to
            # the short ``result_summary`` (also the spoken text for a Mail
            # section, whose renderable content lives in ``result_payload``).
            persisted_result = _sections_markdown_text(structured_payload) or result_summary
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
                self._task_store.set_result(
                    task_id, persisted_result, result_payload=structured_payload
                )
                message_id = self._task_store.append_message(
                    task_id,
                    role="assistant",
                    content=persisted_result,
                    action="done",
                )
                self._task_store.update_state(task_id, "done")
            else:
                # Trou B — opt-in: surface the reason via ``task.result`` so the
                # failed-synthesis (which reads only that column) can explain the
                # failure. Guarded on a non-empty ``result_summary`` so a bare
                # reason-code failure keeps the legacy ``task.result is None``.
                if persist_result_on_failure and result_summary:
                    self._task_store.set_result(task_id, persisted_result)
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

        # Issue 0056 — scrub the Mail subject / bodyPreview / snippet of EACH
        # section before the payload lands in the debug ring buffer + WS / file
        # sinks. The original sections continue to flow through
        # ``task.result_payload`` / the ``task_result`` WS event unchanged; only
        # the debug envelope sees the redacted copy. Non-Mail sections round-trip
        # untouched so Markdown sections stay intact.
        debug_sections = _redact_sections_for_debug(structured_payload)
        # The ``result`` field of the debug payload also carries the LLM's
        # spoken ``result_summary`` which for Mail responses typically
        # contains the subject ("Mail de X, sujet '<subject>', ..."). The
        # frontend already gets that string via the ``task_result`` WS
        # event; duplicating it into the debug envelope only widens the
        # privacy surface for no observability gain. When any section is
        # a Mail descriptor we elide ``result`` and let the per-task
        # overlay derive the summary from ``task.result`` itself.
        is_mail_payload = _sections_contain_mail(structured_payload)
        debug_result_field: str | None = (
            None
            if store_state != "done" or is_mail_payload or redact_result_in_debug
            else persisted_result
        )
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
                "result": debug_result_field,
                "reason": reason_code if store_state != "done" else None,
                "status": status,
                "reason_code": reason_code,
                "ui_payload": debug_sections,
                "cost": cost,
                # Issue 0052: status_change reflection so the overlay
                # can render a terminal pill in the timeline.
                "kind": "status_change",
                "new_state": store_state,
                "schema_version": SUB_AGENT_SCHEMA_VERSION,
            },
        )

        await _emit_task_message(
            self._task_store,
            task_id,
            message_id=message_id,
            redact_content_in_debug=redact_result_in_debug,
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
        # PRD 0008 / issue 0064 — ship the structured deliverable descriptor
        # alongside the spoken/markdown ``result`` text so the frontend
        # task-result effect can dispatch on ``component`` (Mail → MailOverlay,
        # Markdown → MarkdownOverlay) instead of always treating it as
        # markdown. The REAL props travel here (the overlay needs the subject /
        # body to render) — only the debug / JSONL sinks above see the redacted
        # copy. The field is omitted entirely for summary-only / failed tasks
        # so older frontends keep working off ``result``.
        task_result_event: dict[str, Any] = {
            "type": "task_result",
            "task_id": task_id,
            "result": persisted_result,
        }
        if structured_payload is not None:
            task_result_event["result_payload"] = structured_payload
        # The ``ws_events.emit`` shim funnels every WS frame through the
        # unified bus, which ALSO captures it into the debug ring buffer +
        # ``/ws/debug`` feed + JSONL sink. So the real ``result_payload`` we
        # just attached (Mail subject / bodyPreview / snippet) would leak there
        # unless we hand the bus a scrubbed copy. ``debug_task_result_event``
        # redacts the descriptor's email fields and, for a Mail payload, elides
        # the ``result`` text too (it typically embeds the subject) — matching
        # the redaction posture the ``status_change`` debug envelope above
        # already applies. The chat client still receives the unmodified
        # ``task_result_event`` so the overlay renders the full message.
        debug_task_result_event: dict[str, Any] | None = None
        if structured_payload is not None:
            debug_task_result_event = {
                "type": "task_result",
                "task_id": task_id,
                "result": (
                    None if (is_mail_payload or redact_result_in_debug) else persisted_result
                ),
                "result_payload": debug_sections,
            }
        elif redact_result_in_debug:
            # RC2 salvage with no structured payload — keep the salvaged text
            # (which may embed raw tool output / an email body) out of the debug
            # sinks while the chat client still receives the full event.
            debug_task_result_event = {
                "type": "task_result",
                "task_id": task_id,
                "result": None,
            }
        await ws_events.emit(task_result_event, debug_event=debug_task_result_event)
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

    @staticmethod
    def _stall_decision(stall_count: int) -> str:
        """Map a stall streak to ``"force"`` / ``"nudge"`` / ``"none"`` (RC1).

        ``stall_count`` is the run of non-advancing iterations since the last
        successful tool result — a ``progress``, a duplicate ``tool_call``, or a
        ``tool_call`` whose dispatch ERRORED (Trou A/B). At
        :data:`_STALL_FORCE_THRESHOLD` the runner force-terminates via
        :meth:`_force_stalled_done`; at :data:`_STALL_NUDGE_THRESHOLD` it injects
        a ``system_validator`` nudge (:meth:`_stall_nudge_message`). Checked
        force-first so the higher threshold wins.
        """

        if stall_count >= _STALL_FORCE_THRESHOLD:
            return "force"
        if stall_count >= _STALL_NUDGE_THRESHOLD:
            return "nudge"
        return "none"

    @staticmethod
    def _stall_nudge_message(
        *,
        last_tool_result: dict[str, Any] | None,
        last_tool_error: SubAgentToolDispatchResult | None,
    ) -> str:
        """Context-aware ``system_validator`` nudge for a stalling run (RC1 + Trou A/B).

        Three shapes, in priority order:

        - a SUCCESSFUL tool result is in hand → tell the model to stop emitting
          ``progress`` and build the terminal ``done`` from it (original RC1);
        - only a tool ERROR is in hand (Trou B) → name the error and tell the
          model to fix the arguments and retry ONCE, or emit ``done(failed)``;
        - nothing retrieved at all (Trou A — pure ``progress`` spin) → tell the
          model to call a tool or conclude now.
        """

        if last_tool_result is not None:
            return (
                "Tu as déjà un résultat d'outil dans le contexte ci-dessus. "
                "N'émets plus de ``progress`` : émets MAINTENANT une action "
                "``done`` avec le livrable construit à partir de ce résultat. Si "
                "le résultat est vide ou inutilisable, émets ``done`` avec le "
                "statut adéquat."
            )
        if last_tool_error is not None:
            detail = (last_tool_error.error_message or last_tool_error.error_code or "").strip()
            return (
                f"Ton dernier appel à l'outil ``{last_tool_error.tool_name or ''}`` "
                f"a échoué : {detail}. N'émets plus de ``progress`` à vide. Corrige "
                "les arguments et réessaie l'outil UNE seule fois, ou émets "
                '``done`` avec ``status="failed"`` si tu ne peux pas aboutir.'
            )
        return (
            "Tu émets des ``progress`` en boucle sans appeler d'outil ni conclure. "
            "Appelle un outil pour avancer, ou émets MAINTENANT une action "
            '``done`` (``status="failed"`` si la tâche est impossible).'
        )

    async def _force_stalled_done(
        self,
        task_id: str,
        *,
        last_tool_result: dict[str, Any] | None,
        last_tool_name: str | None,
        last_tool_error: SubAgentToolDispatchResult | None,
        result_store: ToolResultStore,
        started_at: float,
        iteration: int,
        tokens_used: int,
    ) -> None:
        """Force-terminate a stalled run with the best content available (RC1 + Trou A/B).

        Three content cases, in priority order:

        - a SUCCESSFUL tool result in hand → ``done(degraded, stalled)`` salvaging
          it (RC2), redacted in debug since it may embed raw tool output (privacy,
          issue 0056);
        - only a tool ERROR in hand (Trou B) → ``done(failed, stalled)`` whose
          ``result_summary`` names the error, so Jarvis can explain the failure
          instead of announcing "aucun résultat";
        - nothing retrieved (Trou A — pure ``progress`` spin) → ``done(failed,
          stalled)`` with an empty summary (the reason code carries the meaning).
        """

        cost = self._build_cost(
            started_at=started_at,
            iterations=iteration,
            tokens_used=tokens_used,
        )
        if last_tool_result is not None:
            # PRD 0009 — pass the store so the projected deliverable (the Mail
            # card) rides the degraded done; the salvaged text still carries the
            # "[résultat partiel]" framing for Jarvis. This is the 2026-05-30 fix:
            # the overlay is no longer empty when the data exists.
            await self._emit_terminal_done(
                task_id,
                status="degraded",
                reason_code=REASON_STALLED,
                result_summary=_salvage_tool_result_text(last_tool_name, last_tool_result),
                cost=cost,
                result_store=result_store,
                redact_result_in_debug=True,
            )
            return
        if last_tool_error is not None:
            detail = (last_tool_error.error_message or last_tool_error.error_code or "").strip()
            tool_label = last_tool_error.tool_name or "outil"
            await self._emit_terminal_done(
                task_id,
                status="failed",
                reason_code=REASON_STALLED,
                result_summary=(
                    f"Échec après plusieurs tentatives — dernière erreur de "
                    f"l'outil {tool_label} : {detail}"
                ).strip(),
                cost=cost,
                # Trou B — let the failed-synthesis read the reason off
                # ``task.result`` instead of an empty "Raison brute".
                persist_result_on_failure=True,
            )
            return
        await self._emit_terminal_done(
            task_id,
            status="failed",
            reason_code=REASON_STALLED,
            result_summary="",
            cost=cost,
        )

    async def _emit_terminal_done(
        self,
        task_id: str,
        *,
        status: SubAgentDoneStatus,
        reason_code: str,
        result_summary: str,
        cost: dict[str, Any],
        result_store: ToolResultStore | None = None,
        redact_result_in_debug: bool = False,
        persist_result_on_failure: bool = False,
    ) -> None:
        """Convenience wrapper around :meth:`_finalize_done` for cap / forced paths.

        PRD 0009 / 0010 — when ``result_store`` is supplied (the cap and stall
        paths that retained data), the deliverable is rebuilt from the store's
        last projection as a list of section descriptors and attached as
        ``sections`` so a card survives a degraded exit (the 2026-05-30 fix). The
        projection's summary backfills ``result_summary`` only when the caller
        passed none — the caller's salvaged text (which carries the degraded
        "[résultat partiel]" framing) still wins when present. Paths with no
        retained data (cancel / wall-clock / hard-kill) pass no store and are
        byte-identical to before.
        """

        sections: list[dict[str, Any]] | None = None
        if result_store is not None:
            deliverable, summary = _resolve_terminal_deliverable(result_store)
            if deliverable is not None:
                sections = deliverable
            if not result_summary and summary:
                result_summary = summary

        await self._finalize_done(
            task_id,
            status=status,
            reason_code=reason_code,
            result_summary=result_summary,
            sections=sections,
            cost=cost,
            redact_result_in_debug=redact_result_in_debug,
            persist_result_on_failure=persist_result_on_failure,
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
    redact_content_in_debug: bool = False,
) -> None:
    """Push a ``task_message`` WS event for a freshly-appended task message.

    ``redact_content_in_debug`` (mail-tool-loop, 2026-05-29): when the message
    body is a SALVAGED tool result (RC2) it may embed raw tool output (e.g. an
    email ``bodyPreview``). The chat client still receives the full content, but
    the debug ring buffer / ``/ws/debug`` feed / JSONL sink get a scrubbed copy
    — mirroring the issue 0056 privacy posture applied to the other done events.
    """

    try:
        for msg in store.get_task_messages(task_id):
            if msg.id != message_id:
                continue
            event: dict[str, Any] = {
                "type": "task_message",
                "task_id": task_id,
                "message_id": msg.id,
                "role": msg.role,
                "content": msg.content,
                "action": msg.action,
                "created_at": msg.created_at,
            }
            debug_event = (
                {**event, "content": _MAIL_REDACTED_PLACEHOLDER}
                if redact_content_in_debug
                else None
            )
            await ws_events.emit(event, debug_event=debug_event)
            return
    except TaskStoreError:
        _logger.exception("sub_agent_runner.emit_task_message_lookup_failed", task_id=task_id)


__all__ = [
    "REASON_HARD_KILLED",
    "REASON_INVALID_OUTPUT",
    "REASON_ITERATION_CAP",
    "REASON_LLM_FAILED",
    "REASON_OK",
    "REASON_STALLED",
    "REASON_TOKEN_CAP",
    "REASON_TOOL_FAILED",
    "REASON_USER_CANCELLED",
    "REASON_WALL_CLOCK_CAP",
    "SubAgentRunner",
]
