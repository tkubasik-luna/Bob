"""Extensible assertion engine for the attestation harness.

PRD 0016 / issue 0098 + Annexe C. An assertion checks a machine-readable
invariant of a scenario run against the captured ``/ws/debug`` event stream.
The full Annexe C vocabulary is large (``fsm_reached``, ``bargein_within_ms``,
``role_used_model``, ``committed_equals_spoken``, ``latency_lt_ms``, ...); this
skeleton implements only the three kinds that are meaningful against TODAY's
text-only Bob and ships a clean registry so each later slice adds its kind in
one place.

Adding a new kind (the extensibility seam)
------------------------------------------

1. Write a function ``(spec, ctx) -> AssertionResult``.
2. Register it: ``register_assertion("fsm_reached", check_fsm_reached)``
   (or decorate it with ``@assertion("fsm_reached")``).

That's it — :class:`bob.attest.runner.ScenarioRunner` dispatches purely on the
``kind`` string, so nothing else changes. An unknown ``kind`` fails loudly with
a ``FAIL`` result naming the missing kind (never a silent pass) so a scenario
referencing a not-yet-built assertion is caught.

Logical events vs debug events
------------------------------

Scenarios speak in *logical* event names (``say``, ``bargein``, ``endpoint``)
that are deliberately decoupled from Bob's internal :class:`DebugEvent`
``category`` / ``source``. :data:`LOGICAL_EVENT_MATCHERS` maps each logical name
to a predicate over the captured event dicts, so a scenario stays stable even
if the underlying emit site is renamed. Today only ``say`` is wired (the
orchestrator's ``output`` reply event); voice logical events land with their
slices.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: A captured ``/ws/debug`` frame — the :meth:`DebugEvent.to_dict` wire shape.
CapturedEvent = dict[str, Any]


@dataclass(frozen=True)
class AssertionResult:
    """Outcome of one assertion — serialised into the verdict's ``assertions``.

    ``kind`` + ``ok`` are always present (Annexe C). ``detail`` is a free-form
    bag of expected/actual fields that the verdict echoes verbatim so a red
    assertion is self-explanatory (e.g. ``{"type": "say", "matched": 0}``).
    """

    kind: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "ok": self.ok}
        out.update(self.detail)
        return out


@dataclass(frozen=True)
class AssertionContext:
    """Everything an assertion is allowed to inspect about a run.

    - ``events`` — every ``/ws/debug`` frame captured during the timeline, in
      arrival order.
    - ``deliverable`` — the run's projected deliverable (for the text path:
      the latest non-empty ``say`` speech). Assertions like
      ``deliverable_nonempty`` read this rather than re-deriving it so the
      projection rule lives in exactly one place
      (:func:`project_deliverable`).
    """

    events: list[CapturedEvent]
    deliverable: str


#: Predicate type: does this captured debug frame count as logical event X?
EventMatcher = Callable[[CapturedEvent], bool]


def _is_say(event: CapturedEvent) -> bool:
    """True for the orchestrator's user-facing reply event with real speech.

    The current Bob emits the assistant reply as a ``category="output"`` debug
    event from ``orchestrator.process_user_message`` carrying ``payload.speech``
    (see :meth:`bob.orchestrator.Orchestrator.process_user_message`). That IS
    the ``say`` of the text path — no voice features required.
    """

    if event.get("category") != "output":
        return False
    payload = event.get("payload") or {}
    speech = payload.get("speech")
    return isinstance(speech, str) and bool(speech.strip())


#: Logical-event → debug-frame predicate. Extended per slice (``bargein``,
#: ``endpoint``, ``user_speaking``, ...). Kept module-level so a later slice
#: registers its matcher next to its assertion kind.
LOGICAL_EVENT_MATCHERS: dict[str, EventMatcher] = {
    "say": _is_say,
}


def project_deliverable(events: list[CapturedEvent]) -> str:
    """Project the run's deliverable from the captured events (text path).

    Mirrors the production "deterministic deliverable projection on every exit
    path" idea (PRD 0010) but at the harness boundary: the deliverable is the
    speech of the LAST ``say`` event captured. Empty string when no reply was
    produced — which is exactly what ``deliverable_nonempty`` then fails on.
    """

    speech = ""
    for event in events:
        if _is_say(event):
            payload = event.get("payload") or {}
            value = payload.get("speech")
            if isinstance(value, str) and value.strip():
                speech = value
    return speech


# --- assertion implementations ----------------------------------------------


def check_event_emitted(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff at least one captured event matches the logical ``type``.

    Spec: ``{kind: event_emitted, type: say}``. An unknown logical ``type`` is
    a hard FAIL naming the type (the scenario asked for an event the harness
    doesn't know how to recognise yet).
    """

    logical_type = spec.get("type")
    if not isinstance(logical_type, str) or not logical_type:
        return AssertionResult(
            kind="event_emitted",
            ok=False,
            detail={"error": "event_emitted requires a 'type' string"},
        )
    matcher = LOGICAL_EVENT_MATCHERS.get(logical_type)
    if matcher is None:
        return AssertionResult(
            kind="event_emitted",
            ok=False,
            detail={
                "type": logical_type,
                "error": f"unknown logical event type {logical_type!r}",
                "known_types": sorted(LOGICAL_EVENT_MATCHERS),
            },
        )
    matched = sum(1 for event in ctx.events if matcher(event))
    return AssertionResult(
        kind="event_emitted",
        ok=matched > 0,
        detail={"type": logical_type, "matched": matched},
    )


def check_no_error_events(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff no captured event carries ``severity == "error"``.

    Spec: ``{kind: no_error_events}``. This is the single most valuable
    invariant on weak local models — it catches an LLM failure, an empty
    completion, an unhandled turn crash, all of which the structlog→debug
    bridge surfaces as ``severity="error"`` system events.
    """

    errors = [
        {"source": event.get("source"), "summary": event.get("summary")}
        for event in ctx.events
        if event.get("severity") == "error"
    ]
    return AssertionResult(
        kind="no_error_events",
        ok=not errors,
        detail={"error_count": len(errors), "errors": errors[:5]},
    )


def check_deliverable_nonempty(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff the projected deliverable is non-empty.

    Spec: ``{kind: deliverable_nonempty}``. Reads :attr:`AssertionContext.deliverable`
    (projected once by :func:`project_deliverable`) so the empty-overlay-on-stall
    failure mode is attestable: a turn that emitted no usable reply fails here.
    """

    deliverable = ctx.deliverable
    return AssertionResult(
        kind="deliverable_nonempty",
        ok=bool(deliverable.strip()),
        detail={"length": len(deliverable)},
    )


# --- registry ----------------------------------------------------------------

#: Assertion implementation type — pure function over (spec, context).
AssertionFn = Callable[[dict[str, Any], AssertionContext], AssertionResult]

_REGISTRY: dict[str, AssertionFn] = {}


def register_assertion(kind: str, fn: AssertionFn) -> None:
    """Register an assertion implementation under ``kind`` (last write wins)."""

    _REGISTRY[kind] = fn


def assertion(kind: str) -> Callable[[AssertionFn], AssertionFn]:
    """Decorator form of :func:`register_assertion`."""

    def _decorate(fn: AssertionFn) -> AssertionFn:
        register_assertion(kind, fn)
        return fn

    return _decorate


def known_kinds() -> list[str]:
    """Return the sorted list of registered assertion kinds (introspection)."""

    return sorted(_REGISTRY)


def run_assertion(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """Dispatch a single assertion spec to its registered implementation.

    An unregistered ``kind`` yields a FAIL result naming the kind — never a
    silent pass — so a scenario referencing a not-yet-implemented assertion is
    caught loudly (and the verdict explains why).
    """

    kind = spec.get("kind")
    if not isinstance(kind, str) or not kind:
        return AssertionResult(
            kind="<missing>", ok=False, detail={"error": "assertion entry has no 'kind'"}
        )
    fn = _REGISTRY.get(kind)
    if fn is None:
        return AssertionResult(
            kind=kind,
            ok=False,
            detail={
                "error": f"assertion kind {kind!r} not implemented yet",
                "known_kinds": known_kinds(),
            },
        )
    return fn(spec, ctx)


# Register the three kinds this skeleton ships. Later slices append their own
# via ``register_assertion`` / ``@assertion`` next to the new logic.
register_assertion("event_emitted", check_event_emitted)
register_assertion("no_error_events", check_no_error_events)
register_assertion("deliverable_nonempty", check_deliverable_nonempty)
