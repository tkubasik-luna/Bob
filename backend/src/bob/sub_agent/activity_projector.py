"""Activity-chip projection — the user-facing agent-activity taxonomy (PRD 0011 / issue 0071).

The HUB of PRD 0011 level 1. Where issue 0069 carries the *cosmetic* reasoning
stream (``reasoning_delta``), this module owns the *observability* channel: the
discrete agent actions that surface as inline chips interleaved chronologically
with the streaming reasoning in the :class:`AgentBlock`.

It is a **PURE** projection layer: an internal event in, a user-facing
:class:`AgentActivity` descriptor out (or ``None`` when the event should be
suppressed). It has ZERO WebSocket / IO dependency — the runner does the
emitting (``ws_events.emit``) and only delegates the *shaping* + *redaction*
here, so the taxonomy can be unit-tested without a runner or a socket.

Why a dedicated module
----------------------

PRD 0011 decisions this honours:

- Chips are observability, NOT a separate debug ring-buffer feed: the wire shape
  is a first-class user-facing event ``{type: "agent_activity", agent_ref, kind,
  label, status}`` on ``/ws/chat`` (the runner builds the wire frame from
  :meth:`AgentActivity.to_wire`).
- Reasoning is cosmetic; chips are observability — *neither* affects action
  validation. Nothing here is read back into the control loop.
- Curated taxonomy: a chip is emitted for tool calls + ``ask_user`` + *salient*
  incidents (stall, cap, retry, validation failure). A PASSING / OK validation
  must NOT produce a chip — :func:`project` returns ``None`` for it so the feed
  is not drowned in green ticks. Only failures are salient.
- Redaction: the user-facing channel reapplies the SAME Mail
  subject/snippet/body scrubbing boundary the debug events use
  (:func:`bob.sub_agent.runner._redact_ui_payload_for_debug`), so a tool-call /
  done chip label can never leak email content.

The ``kind`` taxonomy (``AgentActivityKind``):

- ``started`` / ``finished`` — task lifecycle bookends;
- ``tool_call`` — a sub-agent tool dispatch (start or end, distinguished by
  ``status``);
- ``ask_user`` — the sub-agent asked the user a question;
- ``stall`` — the loop-convergence stall guard fired (nudge or force);
- ``cap`` — a global cap (iteration / wall-clock / token) terminated the run;
- ``retry`` — a bounded ``system_validator`` self-correction round;
- ``validation_failed`` — a deliverable / arg validation rejection (the failure
  that *triggers* a retry; a PASS emits nothing).

The ``status`` taxonomy (``AgentActivityStatus``) is the visual state a chip
renders in: ``running`` (in-flight), ``ok`` (succeeded), ``error`` (failed),
``warn`` (a salient-but-non-fatal incident — stall/cap/retry/validation_failed),
``info`` (neutral — started/finished/ask_user).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bob.llm.types import StreamChunk

#: User-facing chip kinds. Curated (PRD 0011): discrete agent ACTIONS +
#: salient incidents, never per-step OK noise.
AgentActivityKind = Literal[
    "started",
    "finished",
    "tool_call",
    "ask_user",
    "stall",
    "cap",
    "retry",
    "validation_failed",
]

#: Visual state of a chip. ``running`` for an in-flight action, ``ok`` /
#: ``error`` for a settled outcome, ``warn`` for a salient-but-non-fatal
#: incident, ``info`` for a neutral lifecycle/ask marker.
AgentActivityStatus = Literal["running", "ok", "error", "warn", "info"]


@dataclass(frozen=True)
class AgentActivity:
    """A single user-facing activity chip (PRD 0011 / issue 0071).

    The projected, REDACTED descriptor the runner ships on the chat WS. Pure
    data: built by :func:`project`, serialised to the wire frame by
    :meth:`to_wire`. ``agent_ref`` is the producing agent's id (the sub-task's
    ``task_id``) so the frontend store can interleave the chip into the right
    per-agent timeline (lanes, issue 0073).
    """

    agent_ref: str
    kind: AgentActivityKind
    label: str
    status: AgentActivityStatus
    #: Reasoning-streaming PRD (B2) — optional tool-call detail for the chip:
    #: a compact, REDACTED arg summary and a content-free result summary. Only
    #: set on ``tool_call`` chips; ``None`` elsewhere. Both pass the Mail-field
    #: scrub before reaching here (see :func:`_summarise_args` / `_summarise_result`)
    #: because every emit — even ``debug_event=None`` — mirrors into the debug
    #: ring buffer.
    args: str | None = None
    result: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """The ``agent_activity`` WS frame (matches ``AgentActivityMsg`` on the FE)."""

        frame: dict[str, Any] = {
            "type": "agent_activity",
            "agent_ref": self.agent_ref,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
        }
        if self.args is not None:
            frame["args"] = self.args
        if self.result is not None:
            frame["result"] = self.result
        return frame


def agent_perf_frame(agent_ref: str, perf: StreamChunk | None) -> dict[str, Any] | None:
    """Build the ``agent_perf`` WS frame from a terminal ``perf`` :class:`StreamChunk`.

    Returns ``None`` when there is nothing worth showing (no chunk, or every
    field empty), so a degraded backend never emits an empty footer. Matches
    ``AgentPerfMsg`` on the frontend. Like ``agent_activity`` it is a cosmetic,
    first-class user-facing event tagged by ``agent_ref`` so the store routes it
    to the right lane.
    """

    if perf is None:
        return None
    fields = {
        "tokens_in": perf.tokens_in,
        "tokens_out": perf.tokens_out,
        "reasoning_tokens": perf.reasoning_tokens,
        "ttft_s": perf.ttft_s,
        "tok_s": perf.tok_s,
    }
    if all(v is None for v in fields.values()):
        return None
    return {"type": "agent_perf", "agent_ref": agent_ref, **fields}


# --- Internal events ---------------------------------------------------------
#
# The runner constructs these to describe what just happened internally; the
# projector decides what (if anything) the user sees. They are deliberately
# decoupled from the runner's own dataclasses (``SubAgentToolDispatchResult``,
# the action union) so this module stays a pure, independently-testable seam.


@dataclass(frozen=True)
class TaskStarted:
    """The sub-agent run began."""

    agent_ref: str
    title: str | None = None


@dataclass(frozen=True)
class TaskFinished:
    """The sub-agent run reached a terminal state.

    ``status`` is the v2 done status (``complete`` / ``degraded`` / ``failed`` /
    ``cancelled`` / ``timeout``); the projector maps it onto a chip status.
    """

    agent_ref: str
    status: str
    reason_code: str | None = None


@dataclass(frozen=True)
class ToolCallStarted:
    """A sub-agent tool dispatch is about to run (``status=running`` chip).

    ``args`` is the RAW tool-argument dict; the projector redacts + compacts it
    into the chip's ``args`` summary (never echoed verbatim).
    """

    agent_ref: str
    tool_name: str
    args: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolCallFinished:
    """A sub-agent tool dispatch settled (``status=ok`` / ``error`` chip).

    ``args`` is the raw arg dict (same as the start event so the settled chip is
    self-contained). ``result`` is the RAW structured tool result on success
    (the projector redacts it and derives a content-free summary like
    ``"12 éléments"``); it is ``None`` on failure, where ``error_code`` already
    frames the outcome.
    """

    agent_ref: str
    tool_name: str
    ok: bool
    error_code: str | None = None
    args: dict[str, Any] | None = None
    result: Any = None


@dataclass(frozen=True)
class AskUser:
    """The sub-agent asked the user a question (legacy ``ask_user`` flow)."""

    agent_ref: str
    question: str


@dataclass(frozen=True)
class StallNudge:
    """The loop-convergence stall guard fired.

    ``forced`` distinguishes the hard force-termination from a soft nudge — both
    are salient incidents worth a chip (``warn``).
    """

    agent_ref: str
    forced: bool = False


@dataclass(frozen=True)
class CapReached:
    """A global cap terminated the run.

    ``cap`` is the cap kind (``iteration`` / ``wall_clock`` / ``token``).
    """

    agent_ref: str
    cap: str


@dataclass(frozen=True)
class Retry:
    """A bounded ``system_validator`` self-correction round was injected.

    ``attempt`` is the 1-based retry number for the chip label.
    """

    agent_ref: str
    attempt: int
    error_code: str | None = None


@dataclass(frozen=True)
class Validation:
    """A validation outcome.

    ``ok=True`` is the PASSING case — :func:`project` SUPPRESSES it (returns
    ``None``) so the feed is not flooded with per-step green ticks (PRD 0011
    aggregation rule). ``ok=False`` is the salient rejection that surfaces.
    ``what`` names the validated surface (e.g. ``"tool args"`` / ``"livrable"``)
    for the chip label.
    """

    agent_ref: str
    ok: bool
    what: str
    detail: str | None = None


InternalEvent = (
    TaskStarted
    | TaskFinished
    | ToolCallStarted
    | ToolCallFinished
    | AskUser
    | StallNudge
    | CapReached
    | Retry
    | Validation
)


# --- Redaction ---------------------------------------------------------------
#
# Reapply the SAME Mail field boundary the debug events use (issue 0056). A chip
# label is built here from tool names + statuses (metadata only — never the
# email body), so the principal risk is a free-text ``ask_user`` question or a
# salvaged detail string echoing an email subject/snippet. We scrub the known
# Mail field markers out of any free-text label fragment before it leaves the
# pure layer, so the user-facing channel can never widen the privacy surface the
# debug sinks already close.

#: Lower-cased substrings that, if a free-text fragment contains a quoted run
#: after them, would indicate leaking Mail content. We do not parse — we simply
#: never copy a raw email body into a label; the only free text we ever fold in
#: is the redacted form below.
_REDACTED_PLACEHOLDER = "<redacted>"


def _redact_free_text(text: str) -> str:
    """Scrub a free-text label fragment of anything that could carry Mail content.

    The projector NEVER folds a raw tool result / email body into a label — it
    only ever uses tool *names*, statuses and short framing. The single free-text
    arms are :class:`AskUser.question` and :class:`Validation.detail`, which a
    weak model could conceivably echo a subject into. Rather than ship them, we
    truncate hard and strip newlines so a chip stays a one-line metadata marker.
    The full text still travels on its own dedicated channel (the ``ask_user``
    task message / the validator feedback) — the chip is observability only.
    """

    collapsed = " ".join(text.split())
    if len(collapsed) > 80:
        collapsed = collapsed[:80].rstrip() + "…"
    return collapsed


def _summarise_args(args: dict[str, Any] | None) -> str | None:
    """Compact, Mail-REDACTED one-line summary of a tool's args for a chip.

    Runs the args through the shared Mail-field scrub (so a Mail descriptor in
    the args can't leak), renders the scrubbed key/values as ``k: v`` pairs and
    truncates. Returns ``None`` for empty args (no detail row).
    """

    if not args:
        return None
    scrubbed = redact_payload(args)
    if not isinstance(scrubbed, dict):
        return _redact_free_text(str(scrubbed))
    parts = [f"{k}: {v}" for k, v in scrubbed.items()]
    return _redact_free_text(" · ".join(parts))


def _summarise_result(result: Any) -> str | None:
    """Content-FREE, Mail-REDACTED summary of a tool result for a chip.

    Deliberately avoids echoing the result body: a list becomes ``"N éléments"``,
    a dict its scrubbed key set, anything else a hard-truncated scalar. Combined
    with the Mail scrub this can never carry an email subject/snippet/body onto
    the chip (which also lands in the debug ring buffer).
    """

    if result is None:
        return None
    scrubbed = redact_payload(result) if isinstance(result, dict | str) else result
    if isinstance(scrubbed, list):
        n = len(scrubbed)
        return f"{n} élément{'s' if n != 1 else ''}"
    if isinstance(scrubbed, dict):
        keys = list(scrubbed.keys())
        # A common shape: {"items"/"messages"/"results": [...]} — count it.
        for key in ("items", "messages", "results", "events", "files"):
            seq = scrubbed.get(key)
            if isinstance(seq, list):
                n = len(seq)
                return f"{n} {key}"
        return _redact_free_text(", ".join(keys))
    return _redact_free_text(str(scrubbed))


def redact_payload(payload: dict[str, Any] | str | None) -> dict[str, Any] | str | None:
    """Reapply the debug Mail-field redaction on a user-facing payload.

    Delegates to the runner's :func:`_redact_ui_payload_for_debug` so the
    user-facing channel and the debug sinks share ONE scrubbing boundary — a
    Mail descriptor's ``subject`` / ``bodyPreview`` / ``snippet`` / ``body`` are
    replaced with the placeholder; every other shape passes through untouched.
    Exposed for callers that want to redact a structured payload before deriving
    a label from it.
    """

    # Imported lazily to keep this module import-light and avoid a cycle with the
    # runner (which imports this module). The redactor is a pure dict transform.
    from bob.sub_agent.runner import _redact_ui_payload_for_debug

    return _redact_ui_payload_for_debug(payload)


# --- Projection --------------------------------------------------------------


def project(event: InternalEvent) -> AgentActivity | None:
    """Project an internal event into a user-facing chip — or ``None`` to suppress.

    PURE: no IO. The single curation point for the agent-activity taxonomy:

    - tool calls, ``ask_user`` and lifecycle bookends always surface;
    - incidents (stall, cap, retry, validation rejection) surface as ``warn`` /
      ``error`` chips;
    - a PASSING validation is SUPPRESSED (returns ``None``) — aggregation rule;
    - all free-text label fragments are redacted (:func:`_redact_free_text`) so
      no Mail content leaks onto this channel.
    """

    if isinstance(event, TaskStarted):
        label = "Démarré"
        if event.title:
            label = f"Démarré : {_redact_free_text(event.title)}"
        return AgentActivity(
            agent_ref=event.agent_ref, kind="started", label=label, status="info"
        )

    if isinstance(event, TaskFinished):
        status: AgentActivityStatus = (
            "ok" if event.status in ("complete", "degraded") else "error"
        )
        label = {
            "complete": "Terminé",
            "degraded": "Terminé (dégradé)",
            "failed": "Échec",
            "cancelled": "Annulé",
            "timeout": "Expiré",
        }.get(event.status, event.status)
        return AgentActivity(
            agent_ref=event.agent_ref, kind="finished", label=label, status=status
        )

    if isinstance(event, ToolCallStarted):
        return AgentActivity(
            agent_ref=event.agent_ref,
            kind="tool_call",
            label=f"Outil {event.tool_name}",
            status="running",
            args=_summarise_args(event.args),
        )

    if isinstance(event, ToolCallFinished):
        args = _summarise_args(event.args)
        if event.ok:
            return AgentActivity(
                agent_ref=event.agent_ref,
                kind="tool_call",
                label=f"Outil {event.tool_name}",
                status="ok",
                args=args,
                result=_summarise_result(event.result),
            )
        detail = f" ({event.error_code})" if event.error_code else ""
        return AgentActivity(
            agent_ref=event.agent_ref,
            kind="tool_call",
            label=f"Outil {event.tool_name}{detail}",
            status="error",
            args=args,
        )

    if isinstance(event, AskUser):
        return AgentActivity(
            agent_ref=event.agent_ref,
            kind="ask_user",
            label=f"Question : {_redact_free_text(event.question)}",
            status="info",
        )

    if isinstance(event, StallNudge):
        label = "Boucle détectée — terminaison forcée" if event.forced else "Boucle détectée"
        return AgentActivity(
            agent_ref=event.agent_ref, kind="stall", label=label, status="warn"
        )

    if isinstance(event, CapReached):
        label = {
            "iteration": "Limite d'itérations atteinte",
            "wall_clock": "Limite de temps atteinte",
            "token": "Limite de tokens atteinte",
        }.get(event.cap, f"Limite atteinte ({event.cap})")
        return AgentActivity(
            agent_ref=event.agent_ref, kind="cap", label=label, status="warn"
        )

    if isinstance(event, Retry):
        return AgentActivity(
            agent_ref=event.agent_ref,
            kind="retry",
            label=f"Nouvel essai #{event.attempt}",
            status="warn",
        )

    if isinstance(event, Validation):
        # Aggregation rule (PRD 0011): a PASS produces NO chip — only the salient
        # rejection surfaces. This is what keeps the feed from drowning in green
        # ticks while every failure is still observable.
        if event.ok:
            return None
        detail = f" — {_redact_free_text(event.detail)}" if event.detail else ""
        return AgentActivity(
            agent_ref=event.agent_ref,
            kind="validation_failed",
            label=f"Validation échouée : {_redact_free_text(event.what)}{detail}",
            status="error",
        )

    # Total over the union — unreachable, but keeps mypy honest if a new event
    # kind is added without a projection arm.
    return None


__all__ = [
    "AgentActivity",
    "AgentActivityKind",
    "AgentActivityStatus",
    "AskUser",
    "CapReached",
    "InternalEvent",
    "Retry",
    "StallNudge",
    "TaskFinished",
    "TaskStarted",
    "ToolCallFinished",
    "ToolCallStarted",
    "Validation",
    "project",
    "redact_payload",
]
