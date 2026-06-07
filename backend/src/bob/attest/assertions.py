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

import re
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


def _voice_subtype_matcher(subtype: str) -> EventMatcher:
    """Build a matcher for a ``category="voice"`` event with ``payload.type``.

    The « Listen » pipeline (issue 0099) emits its events via
    :func:`bob.event_bus_v2.emit_event`, which nests the wire payload under
    ``payload.ws_event`` in the captured debug frame; the concrete kind is
    ``payload.ws_event.type`` (``stt_partial`` / ``stt_final`` — Annexe A.2).
    This lets ``wait_event`` synchronise on a transcript landing.
    """

    def _match(event: CapturedEvent) -> bool:
        if event.get("category") != "voice":
            return False
        ws_event = (event.get("payload") or {}).get("ws_event") or {}
        return ws_event.get("type") == subtype

    return _match


#: Logical-event → debug-frame predicate. Extended per slice (``bargein``,
#: ``endpoint``, ``user_speaking``, ...). Kept module-level so a later slice
#: registers its matcher next to its assertion kind.
LOGICAL_EVENT_MATCHERS: dict[str, EventMatcher] = {
    "say": _is_say,
    "stt_partial": _voice_subtype_matcher("stt_partial"),
    "stt_final": _voice_subtype_matcher("stt_final"),
    # PRD 0016 / issue 0100 (Annexe A.2 + B): the full-duplex loop's FSM
    # transition event + the outbound TTS chunk marker. ``wait_event`` /
    # ``wait_state`` synchronise on these.
    "turn_state": _voice_subtype_matcher("turn_state"),
    "audio_chunk": _voice_subtype_matcher("audio_chunk"),
    # PRD 0016 / issue 0101 (Annexe A.2 + B): the confirmed barge-in cut.
    # ``wait_event type: bargein`` blocks until Bob was interrupted.
    "bargein": _voice_subtype_matcher("bargein"),
    # PRD 0016 / issue 0110 (Annexe A.2 + F): the per-turn latency summary the
    # loop emits at turn end. ``wait_event type: turn_latency`` blocks until the
    # turn finished + its marks/derived landed; ``latency_lt_ms`` reads them.
    "turn_latency": _voice_subtype_matcher("turn_latency"),
    # PRD 0016 / issue 0110 (Annexe C ``--deep``): the harness-synthesised
    # TTS->STT round-trip observation; ``transcript_roundtrip_similarity_gte``
    # reads it. Present in the matcher table so the kind is a known logical type.
    "roundtrip_transcript": _voice_subtype_matcher("roundtrip_transcript"),
    # PRD 0016 / issue 0102 (Annexe A.2 + H): the background Thinker's snapshot
    # of the turn, and the marker proving the Speaker consulted it at assembly.
    "thinker_snapshot": _voice_subtype_matcher("thinker_snapshot"),
    "thinker_consult": _voice_subtype_matcher("thinker_consult"),
    # PRD 0016 / issue 0109 (Annexe E): the finalized turn was persisted
    # (voice_turns row + audio blobs) and the retention sweep evicted something.
    "voice_turn_persisted": _voice_subtype_matcher("voice_turn_persisted"),
    "voice_retention_purged": _voice_subtype_matcher("voice_retention_purged"),
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


def _llm_call_model(event: CapturedEvent) -> str | None:
    """Return the model of an ``category="llm"`` call event, else ``None``.

    Every real :class:`bob.llm_client.LLMClient` emits a ``category="llm"`` debug
    event carrying ``payload.model`` at each call site (see ``llm_client.py``).
    That is the only place a served model surfaces on the black-box ``/ws/debug``
    stream the harness sees.
    """

    if event.get("category") != "llm":
        return None
    payload = event.get("payload") or {}
    model = payload.get("model")
    return model if isinstance(model, str) and model else None


def check_role_used_model(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff a model named ``spec['model']`` served at least one LLM call.

    Spec: ``{kind: role_used_model, role: jarvis, model: modelA}`` (Annexe C).
    Reads the ``payload.model`` of captured ``category="llm"`` events.

    Scope note (PRD 0016 / issue 0106): per-role clients are not yet wired into
    the app boot, and LLM-call events do not yet carry the role, so this checks
    the *model* invariant (the expected model served a call) rather than the full
    role→model correlation. ``role`` is echoed into the detail and the assertion
    tightens to require a role-tagged event once per-role boot wiring lands (the
    role-picker slice). Under the ``fake`` provider no ``category="llm"`` event is
    emitted, so the end-to-end scenario runs with ``--real`` until then.
    """

    role = spec.get("role")
    expected = spec.get("model")
    if not isinstance(expected, str) or not expected:
        return AssertionResult(
            kind="role_used_model",
            ok=False,
            detail={"error": "role_used_model requires a 'model' string", "role": role},
        )
    models_seen = sorted({m for m in (_llm_call_model(e) for e in ctx.events) if m})
    return AssertionResult(
        kind="role_used_model",
        ok=expected in models_seen,
        detail={"role": role, "expected_model": expected, "models_seen": models_seen},
    )


def check_budget_refused(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff a captured event reports a model-budget refusal (issue 0107).

    Spec: ``{kind: budget_refused}`` (optionally ``{kind: budget_refused,
    role: draft}`` to narrow). Annexe G "Budget dépassé (check)": the per-host
    multi-load policy refuses a role's model BEFORE loading it when the resident
    set would exceed the ceiling. The refusal surfaces on the black-box
    ``/ws/debug`` stream as a system event carrying ``payload.error ==
    "budget_exceeded"`` (the same code the ``PUT /api/llm/roles/{role}`` route
    returns) and/or a ``severity == "error"`` event whose summary mentions the
    "dépasse le plafond" message.

    Scope note (honest seam, mirrors ``role_used_model``): per-role boot/swap
    wiring is not yet attached to the app boot, so a *running-backend* scenario
    that triggers a real over-budget load lands when the role-picker boot wiring
    does (issue 0108). Until then the budget-refusal invariant is attested by a
    focused integration test on the per-role PUT route (``test_role_router.py``
    / ``test_role_swap.py``); this assertion ships the harness vocabulary +
    recognises the refusal event so the e2e scenario is a drop-in once the wire
    exists. Under the ``fake`` provider no such event is emitted, so a scenario
    using it runs ``--real`` (documented), never a faked pass.
    """

    role = spec.get("role")
    matched = [e for e in ctx.events if _is_budget_refusal(e)]
    if isinstance(role, str) and role:
        matched = [e for e in matched if _budget_event_role(e) in (None, role)]
    return AssertionResult(
        kind="budget_refused",
        ok=bool(matched),
        detail={"role": role, "matched": len(matched)},
    )


def _is_budget_refusal(event: CapturedEvent) -> bool:
    """True when ``event`` reports a model-budget refusal (Annexe G).

    Recognised shapes (either suffices): a payload ``error == "budget_exceeded"``
    (the route's structured code, however it is surfaced onto the debug stream),
    or an ``error``-severity event whose summary mentions the "plafond" refusal.
    """

    payload = event.get("payload") or {}
    if isinstance(payload, dict) and payload.get("error") == "budget_exceeded":
        return True
    if event.get("severity") == "error":
        summary = event.get("summary")
        if isinstance(summary, str) and "plafond" in summary:
            return True
    return False


def _budget_event_role(event: CapturedEvent) -> str | None:
    """Best-effort role tag of a budget-refusal event (``None`` when untagged)."""

    payload = event.get("payload") or {}
    role = payload.get("role") if isinstance(payload, dict) else None
    return role if isinstance(role, str) and role else None


def _stt_final_texts(events: list[CapturedEvent]) -> list[str]:
    """Return the ``text`` of every captured ``stt_final`` voice event.

    Note (Privacy, Annexe A.2): the ``/ws/debug`` copy the harness captures
    carries the **scrubbed** transcript — :func:`bob.voice_turn._scrub_text`
    keeps the first ``STT_DEBUG_TEXT_MAX_CHARS`` characters verbatim then
    elides. A scenario therefore asserts a substring within that leading
    window (the demo fixture keeps the transcript short so it survives whole).
    """

    matcher = _voice_subtype_matcher("stt_final")
    texts: list[str] = []
    for event in events:
        if not matcher(event):
            continue
        ws_event = (event.get("payload") or {}).get("ws_event") or {}
        text = ws_event.get("text")
        if isinstance(text, str):
            texts.append(text)
    return texts


def check_stt_final_matches(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff some ``stt_final`` transcript matches ``contains`` / ``regex``.

    Spec: ``{kind: stt_final_matches, contains: "..."}`` or
    ``{kind: stt_final_matches, regex: "..."}`` (Annexe C — audio mode). Asserts
    the *contract* (the expected words were transcribed), never an exact string,
    so a fuzzy real-STT run stays robust. Reads the scrubbed ``/ws/debug`` copy
    (see :func:`_stt_final_texts`).
    """

    contains = spec.get("contains")
    regex = spec.get("regex")
    if not isinstance(contains, str) and not isinstance(regex, str):
        return AssertionResult(
            kind="stt_final_matches",
            ok=False,
            detail={"error": "stt_final_matches requires a 'contains' or 'regex' string"},
        )
    finals = _stt_final_texts(ctx.events)
    if not finals:
        return AssertionResult(
            kind="stt_final_matches",
            ok=False,
            detail={"error": "no stt_final event captured", "finals": []},
        )
    if isinstance(contains, str):
        ok = any(contains in text for text in finals)
        criterion: dict[str, Any] = {"contains": contains}
    else:
        assert isinstance(regex, str)
        pattern = re.compile(regex)
        ok = any(pattern.search(text) is not None for text in finals)
        criterion = {"regex": regex}
    return AssertionResult(
        kind="stt_final_matches",
        ok=ok,
        detail={**criterion, "finals": finals[:5]},
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


# --- full-duplex loop assertions (PRD 0016 / issue 0100) ---------------------


def _turn_state_events(events: list[CapturedEvent]) -> list[dict[str, Any]]:
    """Return the ``ws_event`` body of every captured ``turn_state`` voice event.

    Each body is the Annexe A.2 ``turn_state`` payload
    (``{turn_id, from, to, reason, ts, type}``) the loop emits via
    :func:`bob.event_bus_v2.emit_event` (nested under ``payload.ws_event``).
    """

    matcher = _voice_subtype_matcher("turn_state")
    out: list[dict[str, Any]] = []
    for event in events:
        if not matcher(event):
            continue
        ws_event = (event.get("payload") or {}).get("ws_event") or {}
        if isinstance(ws_event, dict):
            out.append(ws_event)
    return out


def check_fsm_reached(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff a ``turn_state`` transition reached ``spec['state']`` (Annexe C).

    Spec: ``{kind: fsm_reached, state: bob_speaking}``. Reads the ``to`` field of
    the captured ``turn_state`` voice events (PRD 0016 / Annexe B). This asserts
    the FSM *visited* the target state at some point in the run — the invariant a
    slice cares about ("Bob got the floor", "the user was heard") — not the exact
    transition path. An optional ``turn_id`` narrows the check to one turn.
    """

    state = spec.get("state")
    if not isinstance(state, str) or not state:
        return AssertionResult(
            kind="fsm_reached",
            ok=False,
            detail={"error": "fsm_reached requires a 'state' string"},
        )
    want_turn = spec.get("turn_id")
    transitions = _turn_state_events(ctx.events)
    states_reached = sorted(
        {
            str(t.get("to"))
            for t in transitions
            if not (isinstance(want_turn, str) and want_turn) or t.get("turn_id") == want_turn
        }
    )
    return AssertionResult(
        kind="fsm_reached",
        ok=state in states_reached,
        detail={"state": state, "states_reached": states_reached},
    )


def check_audio_chunks_gte(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff at least ``spec['min']`` outbound ``audio_chunk`` events were seen.

    Spec: ``{kind: audio_chunks_gte, min: 1}`` (Annexe C). Counts the
    ``audio_chunk`` voice events the say-path emits per outbound PCM block (the
    raw PCM rides the chat socket, so the count is the debug-observable proxy for
    "Bob actually spoke"). Covers the bare-loop audio-out criterion; a later
    barge-in slice asserts a *cut* count against the same events.
    """

    raw_min = spec.get("min", 1)
    try:
        minimum = int(raw_min)
    except (TypeError, ValueError):
        return AssertionResult(
            kind="audio_chunks_gte",
            ok=False,
            detail={"error": "audio_chunks_gte 'min' must be an integer", "min": raw_min},
        )
    matcher = _voice_subtype_matcher("audio_chunk")
    count = sum(1 for event in ctx.events if matcher(event))
    return AssertionResult(
        kind="audio_chunks_gte",
        ok=count >= minimum,
        detail={"min": minimum, "count": count},
    )


# Register the kinds this module ships. Later slices append their own via
# ``register_assertion`` / ``@assertion`` next to the new logic.
register_assertion("event_emitted", check_event_emitted)
register_assertion("no_error_events", check_no_error_events)
register_assertion("deliverable_nonempty", check_deliverable_nonempty)
register_assertion("role_used_model", check_role_used_model)
register_assertion("budget_refused", check_budget_refused)
register_assertion("stt_final_matches", check_stt_final_matches)
register_assertion("fsm_reached", check_fsm_reached)
register_assertion("audio_chunks_gte", check_audio_chunks_gte)


# --- barge-in assertions (PRD 0016 / issue 0101) -----------------------------


def _bargein_events(events: list[CapturedEvent]) -> list[dict[str, Any]]:
    """Return the ``ws_event`` body of every captured ``bargein`` voice event.

    Each body is the Annexe A.2 ``bargein`` payload
    (``{turn_id, detected_ts, cut_ts, committed_spoken_text}``). The
    ``committed_spoken_text`` here is the **scrubbed** ring-buffer copy (Privacy,
    Annexe A.2) — a scenario keeps its reply short so it survives the leading
    window whole.
    """

    matcher = _voice_subtype_matcher("bargein")
    out: list[dict[str, Any]] = []
    for event in events:
        if not matcher(event):
            continue
        ws_event = (event.get("payload") or {}).get("ws_event") or {}
        if isinstance(ws_event, dict):
            out.append(ws_event)
    return out


def check_bargein_within_ms(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff a barge-in was cut within ``spec['max']`` ms (Annexe B + F).

    Spec: ``{kind: bargein_within_ms, max: 300}`` (Annexe C). The cut latency is
    ``cut_ts - detected_ts`` of the captured ``bargein`` event (seconds → ms) —
    exactly the Annexe F derived ``bargein_cut_ms`` (target <300). FAILs loudly
    when no ``bargein`` event was captured (Bob was never cut) so a scenario that
    expected an interrupt and got none is red, not silently green.
    """

    raw_max = spec.get("max", 300)
    try:
        maximum = float(raw_max)
    except (TypeError, ValueError):
        return AssertionResult(
            kind="bargein_within_ms",
            ok=False,
            detail={"error": "bargein_within_ms 'max' must be a number", "max": raw_max},
        )
    bargeins = _bargein_events(ctx.events)
    if not bargeins:
        return AssertionResult(
            kind="bargein_within_ms",
            ok=False,
            detail={"error": "no bargein event captured", "expected_max": maximum},
        )
    # Best (smallest) cut across captured barge-ins; a scenario triggers one.
    measured: list[float] = []
    for ev in bargeins:
        detected = ev.get("detected_ts")
        cut = ev.get("cut_ts")
        if isinstance(detected, (int, float)) and isinstance(cut, (int, float)):
            measured.append(round((float(cut) - float(detected)) * 1000.0, 3))
    if not measured:
        return AssertionResult(
            kind="bargein_within_ms",
            ok=False,
            detail={"error": "bargein event missing detected_ts/cut_ts", "expected_max": maximum},
        )
    actual = min(measured)
    return AssertionResult(
        kind="bargein_within_ms",
        ok=actual <= maximum,
        detail={"expected_max": maximum, "actual": actual},
    )


def _norm_spoken(text: str) -> str:
    """Normalise spoken text for prefix comparison (collapse whitespace, casefold).

    The committed text is reconstructed from cleaned TTS sentences while the
    deliverable is the orchestrator's reply; both pass through here so the
    ``committed_equals_spoken`` check is robust to trailing/internal whitespace
    and case differences rather than asserting a brittle exact string.
    """

    return " ".join(text.split()).casefold()


def check_committed_equals_spoken(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff the barge-in's committed text matches what Bob actually played.

    Spec: ``{kind: committed_equals_spoken}`` (Annexe C — "texte committé ==
    prononcé"). The invariant: the ``committed_spoken_text`` of the captured
    ``bargein`` event is a **non-empty prefix** of Bob's full reply (the ``say``
    deliverable). A prefix — not equality — because a barge-in cuts Bob
    mid-reply, so he committed the *played leading portion*, never the whole
    thing and never the un-played tail. This is the decode-fidelity-independent
    contract (it would still hold under a real TTS that plays the same prefix).

    FAILs when there is no ``bargein`` event, when its committed text is empty
    (nothing was played but a cut fired — a bug), or when the committed text is
    not a prefix of the reply (Bob committed text he did not play).
    """

    bargeins = _bargein_events(ctx.events)
    if not bargeins:
        return AssertionResult(
            kind="committed_equals_spoken",
            ok=False,
            detail={"error": "no bargein event captured"},
        )
    committed = ""
    for ev in bargeins:
        value = ev.get("committed_spoken_text")
        if isinstance(value, str) and value.strip():
            committed = value
            break
    if not committed.strip():
        return AssertionResult(
            kind="committed_equals_spoken",
            ok=False,
            detail={"error": "committed_spoken_text empty", "committed": committed},
        )
    deliverable = ctx.deliverable
    # The /ws/debug copy is scrubbed (Privacy, Annexe A.2): text over the debug
    # window arrives elided as ``"<window>… [+N chars]"``. Recover the verbatim
    # leading window so the prefix check works regardless of played length.
    verbatim = _strip_scrub_elision(committed)
    norm_committed = _norm_spoken(verbatim)
    norm_deliverable = _norm_spoken(deliverable)
    ok = bool(norm_committed) and norm_deliverable.startswith(norm_committed)
    return AssertionResult(
        kind="committed_equals_spoken",
        ok=ok,
        detail={"committed": committed, "deliverable_prefix": deliverable[:64]},
    )


#: Matches the elision suffix :func:`bob.voice_turn._scrub_text` appends.
_SCRUB_ELISION_RE = re.compile(r"…\s*\[\+\d+ chars\]\s*$")


def _strip_scrub_elision(text: str) -> str:
    """Return the verbatim leading window of a (possibly) scrubbed transcript.

    :func:`bob.voice_turn._scrub_text` keeps the first N chars verbatim then
    appends ``"… [+M chars]"``. We strip that suffix so the kept prefix can be
    matched against the full reply; a non-elided string is returned unchanged.
    """

    return _SCRUB_ELISION_RE.sub("", text)


register_assertion("bargein_within_ms", check_bargein_within_ms)
register_assertion("committed_equals_spoken", check_committed_equals_spoken)


# --- latency assertions (PRD 0016 / issue 0110, Annexe C + F) ----------------


def _turn_latency_events(events: list[CapturedEvent]) -> list[dict[str, Any]]:
    """Return the ``ws_event`` body of every captured ``turn_latency`` voice event.

    Each body is the Annexe A.2 / F summary the loop emits at turn end:
    ``{type, turn_id, marks, derived, ts}`` (nested under ``payload.ws_event``).
    ``marks`` is a ``{name: monotone_seconds}`` map of only the marks a slice
    stamped this turn; ``derived`` is the ms-delta / bool projection. This is the
    single source ``latency_lt_ms`` reads — the same marks the persisted
    ``voice_turns.latency_json`` carries (both come from
    :meth:`bob.latency.TurnLatency.as_event_body`).
    """

    matcher = _voice_subtype_matcher("turn_latency")
    out: list[dict[str, Any]] = []
    for event in events:
        if not matcher(event):
            continue
        ws_event = (event.get("payload") or {}).get("ws_event") or {}
        if isinstance(ws_event, dict):
            out.append(ws_event)
    return out


def check_latency_lt_ms(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff some turn's ``to_mark - from_mark`` is ``<= spec['max']`` ms.

    Spec: ``{kind: latency_lt_ms, from_mark: t_endpoint, to_mark: t_first_audio_chunk,
    max: 800}`` (Annexe C + F). Reads the ``marks`` of the captured
    ``turn_latency`` voice events (monotone seconds) and computes the delta in
    milliseconds — exactly the Annexe F derived (e.g. ``endpoint_to_first_audio_ms
    < 800`` committed / ``< 1500`` cold). The BEST (smallest) delta across every
    turn that carried BOTH marks is checked, so a multi-turn run passes when at
    least one turn met the target.

    FAILs loudly — never a silent green — when: ``from_mark`` / ``to_mark`` are
    missing from the spec; no ``turn_latency`` event was captured (no turn
    finished); or no captured turn carried BOTH marks (the measured span never
    occurred — e.g. asserting ``t_first_audio_chunk`` on a turn where Bob never
    spoke). The detail echoes the marks each turn DID carry so a red assertion is
    self-explanatory.
    """

    from_mark = spec.get("from_mark")
    to_mark = spec.get("to_mark")
    if not isinstance(from_mark, str) or not from_mark:
        return AssertionResult(
            kind="latency_lt_ms",
            ok=False,
            detail={"error": "latency_lt_ms requires a 'from_mark' string"},
        )
    if not isinstance(to_mark, str) or not to_mark:
        return AssertionResult(
            kind="latency_lt_ms",
            ok=False,
            detail={"error": "latency_lt_ms requires a 'to_mark' string"},
        )
    raw_max = spec.get("max")
    try:
        maximum = float(raw_max)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return AssertionResult(
            kind="latency_lt_ms",
            ok=False,
            detail={"error": "latency_lt_ms requires a numeric 'max'", "max": raw_max},
        )

    summaries = _turn_latency_events(ctx.events)
    if not summaries:
        return AssertionResult(
            kind="latency_lt_ms",
            ok=False,
            detail={
                "error": "no turn_latency event captured",
                "from_mark": from_mark,
                "to_mark": to_mark,
                "expected_max": maximum,
            },
        )
    measured: list[float] = []
    marks_seen: list[list[str]] = []
    for body in summaries:
        marks = body.get("marks") or {}
        if not isinstance(marks, dict):
            continue
        marks_seen.append(sorted(str(k) for k in marks))
        start = marks.get(from_mark)
        end = marks.get(to_mark)
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            measured.append(round((float(end) - float(start)) * 1000.0, 3))
    if not measured:
        return AssertionResult(
            kind="latency_lt_ms",
            ok=False,
            detail={
                "error": f"no turn carried both marks {from_mark!r} and {to_mark!r}",
                "from_mark": from_mark,
                "to_mark": to_mark,
                "expected_max": maximum,
                "marks_seen": marks_seen[:5],
            },
        )
    actual = min(measured)
    return AssertionResult(
        kind="latency_lt_ms",
        ok=actual <= maximum,
        detail={
            "from_mark": from_mark,
            "to_mark": to_mark,
            "expected_max": maximum,
            "actual": actual,
            "all_ms": measured[:5],
        },
    )


register_assertion("latency_lt_ms", check_latency_lt_ms)


# --- deep (round-trip) assertions (PRD 0016 / issue 0110, Annexe C --deep) ----


def _norm_for_similarity(text: str) -> str:
    """Normalise text for the round-trip similarity ratio (issue 0110).

    Collapses whitespace, casefolds, and strips trailing punctuation noise so the
    comparison reflects *what was said*, not formatting. The same normalisation
    is applied to both the said text and the re-transcribed text so the ratio is
    a fair word-level overlap.
    """

    lowered = " ".join(text.split()).casefold()
    return "".join(ch for ch in lowered if ch.isalnum() or ch.isspace()).strip()


def _similarity_ratio(said: str, heard: str) -> float:
    """Return a 0..1 similarity between the said and the re-transcribed text.

    Uses :class:`difflib.SequenceMatcher` over the normalised strings — a
    decode-fidelity-tolerant ratio (1.0 == identical after normalisation). Two
    empty strings are treated as a perfect match (1.0); one empty and one not is
    0.0. This is the metric ``transcript_roundtrip_similarity_gte`` thresholds.
    """

    import difflib

    a = _norm_for_similarity(said)
    b = _norm_for_similarity(heard)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def check_transcript_roundtrip_similarity_gte(
    spec: dict[str, Any], ctx: AssertionContext
) -> AssertionResult:
    """PASS iff a TTS→STT round-trip preserved the reply (``--deep`` only).

    Spec: ``{kind: transcript_roundtrip_similarity_gte, min: 0.6}`` (Annexe C
    ``--deep``). The DEEP scenario re-records Bob's spoken reply: the say-path's
    TTS audio is fed back through whisper, and the re-transcription is emitted as
    a ``roundtrip_transcript`` voice event ``{said, heard, similarity}``. This
    assertion reads that event and checks the similarity ``>= min`` — proving the
    end-to-end audio path (synthesise → play → hear) is faithful, not merely that
    bytes flowed. Under the deterministic fake TTS/STT the round-trip is exact
    (the fake STT converges to the said text), so the assertion is stable in CI;
    against real models it tolerates decode fuzz via the ratio.

    The harness computes the ratio at emit time and also carries the raw ``said``
    / ``heard`` so the assertion can recompute defensively (a malformed
    pre-computed ``similarity`` falls back to recomputation). FAILs loudly when
    the run carried no ``roundtrip_transcript`` event — i.e. ``--deep`` was not
    enabled, or no reply was spoken — so a non-deep run never silently passes a
    deep assertion.
    """

    raw_min = spec.get("min", 0.6)
    try:
        minimum = float(raw_min)
    except (TypeError, ValueError):
        return AssertionResult(
            kind="transcript_roundtrip_similarity_gte",
            ok=False,
            detail={"error": "transcript_roundtrip_similarity_gte 'min' must be a number"},
        )
    roundtrips = _voice_ws_events(ctx.events, "roundtrip_transcript")
    if not roundtrips:
        return AssertionResult(
            kind="transcript_roundtrip_similarity_gte",
            ok=False,
            detail={
                "error": "no roundtrip_transcript event captured (run with --deep?)",
                "expected_min": minimum,
            },
        )
    # Best similarity across captured round-trips. Prefer the harness-computed
    # value; recompute from said/heard when it is absent or non-numeric.
    best = -1.0
    best_detail: dict[str, Any] = {}
    for ev in roundtrips:
        said = ev.get("said")
        heard = ev.get("heard")
        precomputed = ev.get("similarity")
        if isinstance(precomputed, (int, float)):
            ratio = float(precomputed)
        elif isinstance(said, str) and isinstance(heard, str):
            ratio = _similarity_ratio(said, heard)
        else:
            continue
        if ratio > best:
            best = ratio
            best_detail = {
                "said": said if isinstance(said, str) else None,
                "heard": heard if isinstance(heard, str) else None,
            }
    if best < 0.0:
        return AssertionResult(
            kind="transcript_roundtrip_similarity_gte",
            ok=False,
            detail={
                "error": "roundtrip_transcript event missing said/heard/similarity",
                "expected_min": minimum,
            },
        )
    return AssertionResult(
        kind="transcript_roundtrip_similarity_gte",
        ok=best >= minimum,
        detail={"expected_min": minimum, "similarity": round(best, 4), **best_detail},
    )


register_assertion("transcript_roundtrip_similarity_gte", check_transcript_roundtrip_similarity_gte)


# --- Thinker assertions (PRD 0016 / issue 0102) ------------------------------


def _voice_ws_events(events: list[CapturedEvent], subtype: str) -> list[dict[str, Any]]:
    """Return the ``ws_event`` body of every captured voice event of ``subtype``."""

    matcher = _voice_subtype_matcher(subtype)
    out: list[dict[str, Any]] = []
    for event in events:
        if not matcher(event):
            continue
        ws_event = (event.get("payload") or {}).get("ws_event") or {}
        if isinstance(ws_event, dict):
            out.append(ws_event)
    return out


def check_thinker_snapshot_emitted(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff at least ``spec['min']`` (default 1) ``thinker_snapshot`` events fired.

    Spec: ``{kind: thinker_snapshot_emitted}`` or ``{kind: thinker_snapshot_emitted,
    min: 2}`` (Annexe A.2 + H). Counts the background Thinker's snapshots on the
    ``/ws/debug`` stream — the proof the « Penser en parallèle » loop ran on the
    partial transcript during the turn. FAILs loudly when none was captured (the
    loop never produced an understanding) so a green run truly exercised it.
    """

    raw_min = spec.get("min", 1)
    try:
        minimum = int(raw_min)
    except (TypeError, ValueError):
        return AssertionResult(
            kind="thinker_snapshot_emitted",
            ok=False,
            detail={"error": "thinker_snapshot_emitted 'min' must be an integer", "min": raw_min},
        )
    snapshots = _voice_ws_events(ctx.events, "thinker_snapshot")
    return AssertionResult(
        kind="thinker_snapshot_emitted",
        ok=len(snapshots) >= minimum,
        detail={"min": minimum, "count": len(snapshots)},
    )


def check_speaker_consulted_thinker(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff the Speaker consulted a Thinker snapshot at assembly (issue 0102).

    Spec: ``{kind: speaker_consulted_thinker}`` (acceptance: "the Speaker's
    assembled context contained the snapshot"). The orchestrator emits a
    dedicated ``thinker_consult`` voice marker (carrying the consulted
    ``turn_id`` / ``seq``) ONLY when the ``thinker_state`` provider actually
    folded a snapshot into the assembled prompt — so this assertion proves the
    snapshot reached the say-path's context, not merely that a snapshot event
    fired. FAILs when no consult marker was captured.
    """

    consults = _voice_ws_events(ctx.events, "thinker_consult")
    return AssertionResult(
        kind="speaker_consulted_thinker",
        ok=bool(consults),
        detail={"consults": len(consults), "seqs": [c.get("seq") for c in consults][:5]},
    )


register_assertion("thinker_snapshot_emitted", check_thinker_snapshot_emitted)
register_assertion("speaker_consulted_thinker", check_speaker_consulted_thinker)


# --- voice persistence + retention assertions (PRD 0016 / issue 0109) --------


def check_voice_turn_persisted(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff a finalized voice turn was persisted (Annexe E).

    Spec: ``{kind: voice_turn_persisted}`` or with optional filters
    ``{min: 1, min_blobs: 1, end_reason: completed}``. Counts the
    ``voice_turn_persisted`` voice events the WS persist hook emits — the
    black-box proof the ``voice_turns`` row + audio blobs were written (the row
    + file themselves are off the wire; the event carries ``blob_count`` /
    ``end_reason`` / ``has_transcript`` so the harness asserts without reaching
    into the DB). ``min_blobs`` requires at least one matching event to carry
    that many blobs; ``end_reason`` filters to a specific finalize kind. FAILs
    loudly when nothing was persisted (the finalize path never wrote a turn).
    """

    raw_min = spec.get("min", 1)
    raw_min_blobs = spec.get("min_blobs")
    want_end_reason = spec.get("end_reason")
    try:
        minimum = int(raw_min)
    except (TypeError, ValueError):
        return AssertionResult(
            kind="voice_turn_persisted",
            ok=False,
            detail={"error": "voice_turn_persisted 'min' must be an integer", "min": raw_min},
        )

    events = _voice_ws_events(ctx.events, "voice_turn_persisted")
    if isinstance(want_end_reason, str) and want_end_reason:
        events = [e for e in events if e.get("end_reason") == want_end_reason]

    ok = len(events) >= minimum
    detail: dict[str, Any] = {
        "min": minimum,
        "count": len(events),
        "blob_counts": [e.get("blob_count") for e in events][:5],
        "end_reasons": [e.get("end_reason") for e in events][:5],
    }
    if want_end_reason is not None:
        detail["end_reason"] = want_end_reason
    if raw_min_blobs is not None:
        try:
            min_blobs = int(raw_min_blobs)
        except (TypeError, ValueError):
            return AssertionResult(
                kind="voice_turn_persisted",
                ok=False,
                detail={"error": "voice_turn_persisted 'min_blobs' must be an integer"},
            )
        blobs_ok = any(int(e.get("blob_count") or 0) >= min_blobs for e in events)
        ok = ok and blobs_ok
        detail["min_blobs"] = min_blobs

    return AssertionResult(kind="voice_turn_persisted", ok=ok, detail=detail)


def check_voice_retention_purged(spec: dict[str, Any], ctx: AssertionContext) -> AssertionResult:
    """PASS iff the retention sweep evicted at least ``spec['min_blobs']`` blobs.

    Spec: ``{kind: voice_retention_purged}`` or ``{min_blobs: 1}`` (Annexe E.3).
    Counts the ``voice_retention_purged`` voice events the persist hook emits
    when :func:`bob.voice_retention_policy.enforce` actually deleted something —
    the proof a forced-tiny cap evicted the oldest audio (file + row). The
    summed ``blobs_deleted`` across the captured purge events must meet the
    threshold (default 1). FAILs when no purge fired (nothing was over the cap,
    so the scenario didn't exercise eviction).
    """

    raw_min_blobs = spec.get("min_blobs", 1)
    try:
        min_blobs = int(raw_min_blobs)
    except (TypeError, ValueError):
        return AssertionResult(
            kind="voice_retention_purged",
            ok=False,
            detail={"error": "voice_retention_purged 'min_blobs' must be an integer"},
        )
    purges = _voice_ws_events(ctx.events, "voice_retention_purged")
    total_blobs = sum(int(p.get("blobs_deleted") or 0) for p in purges)
    total_turns = sum(int(p.get("turns_deleted") or 0) for p in purges)
    return AssertionResult(
        kind="voice_retention_purged",
        ok=total_blobs >= min_blobs,
        detail={
            "min_blobs": min_blobs,
            "purge_events": len(purges),
            "blobs_deleted": total_blobs,
            "turns_deleted": total_turns,
        },
    )


register_assertion("voice_turn_persisted", check_voice_turn_persisted)
register_assertion("voice_retention_purged", check_voice_retention_purged)
