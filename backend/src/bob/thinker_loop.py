"""ThinkerLoop — the « Penser en parallèle » background reasoning pass.

PRD 0016 / issue 0102 (the « Penser en parallèle » étage + Annexe A.2 + H). While
the user is still speaking, the full-duplex loop feeds every ``stt_partial`` to a
ThinkerLoop running on a MINI model (the ``thinker`` role, default a small local
model — see :func:`bob.llm.factory.build_thinker_role_client`). Each pass reads
the partial transcript and produces a :class:`bob.live_transcript_state.ThinkerSnapshot`:

- a state read ``{corrected_text, variables, next_step_plan}`` (Annexe A.2),
- the ``user_turn_complete`` semantic-endpoint signal (CARRIED here, consumed in
  S7/0103 — the VAD silence floor stays the endpoint net),
- a ``backchannel`` trigger (CARRIED here, consumed in S10/0105).

The snapshot lands in the per-session :class:`bob.live_transcript_state.LiveTranscriptState`,
where the pure :class:`bob.context.providers.thinker_state.ThinkerStateProvider`
reads it at prompt-assembly so the Speaker answers from the freshest
understanding.

Cadence (Annexe H, normative)
-----------------------------

- Re-trigger on a new ``stt_partial`` but **debounced** (``THINKER_DEBOUNCE_MS``,
  default 250 ms): a partial that arrives within the window of the last accepted
  trigger only updates the "latest partial" — the loop does not start a fresh
  inference per partial.
- **At most ONE inference in flight per turn.** A partial that arrives while a
  pass is running sets a "rerun pending" flag with the newest text; the loop
  re-evaluates exactly once when the current pass finishes (so the model always
  re-runs against the most recent transcript, never a backlog of stale ones).
- Each accepted snapshot carries a strictly increasing ``seq``; the store IGNORES
  an out-of-order (stale) ``seq`` (anti-stale) — so a longer pass that lands
  after a shorter later one cannot regress the understanding.

Lifecycle + cooperative cancellation (Annexe H)
-----------------------------------------------

:meth:`start` arms the loop for a turn (fresh ``seq`` from 0, clears the store).
:meth:`feed_partial` is the per-``stt_partial`` hook. On ``endpoint`` /
``bargein`` / ``voice_stop`` the full-duplex loop calls :meth:`stop`, which mirrors
the sub-agent cancel ladder (:mod:`bob.task_scheduler`): set a cooperative stop
flag, give the in-flight inference ``THINKER_CANCEL_GRACE_MS`` to unwind, then
escalate to :meth:`asyncio.Task.cancel`. The inference task is spawned through an
injected ``spawn`` callable so the WS layer can route it onto the scheduler's
shared :class:`asyncio.TaskGroup` (structured concurrency — no leaked
background coroutine on an orchestrator crash); tests pass a bare
``asyncio.create_task``.

Why a thin imperative shell over a pure snapshot? The snapshot projection +
anti-stale ordering live in the pure
:class:`bob.live_transcript_state.LiveTranscriptState` / provider; this module is
the effectful part (the LLM call, the debounce timing, the WS event, the
cancellation) — kept deliberately separate so the read path stays testable and
the assembly stays pure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import structlog

from bob.config import Settings
from bob.event_bus_v2 import emit_event
from bob.live_transcript_state import LiveTranscriptState, ThinkerSnapshot
from bob.llm_client import LLMClient
from bob.voice_turn import _scrub_text

_logger = structlog.get_logger(__name__)

#: Factory the loop uses to spawn its inference coroutine. The WS layer wires the
#: scheduler's :meth:`asyncio.TaskGroup.create_task` here (structured
#: concurrency); tests pass :func:`asyncio.create_task`. Typed as a coroutine
#: (not a bare awaitable) so it drops straight into ``TaskGroup.create_task``.
SpawnTask = Callable[[Coroutine[Any, Any, None]], "asyncio.Task[None]"]


#: The system prompt for the mini Thinker model. It asks for a STRICT JSON object
#: matching the snapshot contract (Annexe A.2). Kept terse — the model is small
#: and latency-critical. The schema below gates the decode on a guided backend.
_THINKER_SYSTEM_PROMPT = (
    "Tu es le module de COMPRÉHENSION en arrière-plan d'un assistant vocal. "
    "On te donne la transcription PARTIELLE de ce que l'utilisateur est en train "
    "de dire (elle peut être incomplète). Sans jamais répondre à l'utilisateur, "
    "tu maintiens une lecture structurée de son intention.\n"
    "Réponds UNIQUEMENT par un objet JSON, sans texte autour, conforme à : "
    '{"corrected_text": string (la phrase nettoyée/ponctuée telle que tu la '
    'comprends jusqu\'ici), "variables": object (intentions/entités/paramètres '
    'extraits, {} si rien), "next_step_plan": string (en une phrase, ce que '
    'l\'assistant devrait faire ensuite), "user_turn_complete": boolean (true si '
    "la phrase de l'utilisateur te paraît sémantiquement terminée), "
    '"backchannel": string|null (un bref accusé de réception type "mm", "ok", '
    "ou null si rien)}."
)


def thinker_snapshot_response_schema() -> dict[str, object]:
    """JSON Schema for the snapshot, used as ``response_format`` on a guided backend.

    Derived once (single source) so a guided ``thinker`` model (LM Studio) is
    token-gated to the exact snapshot shape — a fenced / prose-wrapped reply is
    impossible by construction. A non-guided backend (Claude CLI) ignores it and
    we fall back to the tolerant :func:`_parse_snapshot_json` decode.
    """

    return {
        "name": "thinker_snapshot",
        "schema": {
            "type": "object",
            "properties": {
                "corrected_text": {"type": "string"},
                "variables": {"type": "object"},
                "next_step_plan": {"type": "string"},
                "user_turn_complete": {"type": "boolean"},
                "backchannel": {"type": ["string", "null"]},
            },
            "required": ["corrected_text"],
            "additionalProperties": True,
        },
    }


class ThinkerLoop:
    """Per-session background Thinker — debounced, ≤1 inference in flight per turn.

    Construct with the ``thinker`` role :class:`LLMClient` (mini model), the
    per-session :class:`LiveTranscriptState` the snapshots land in, the resolved
    :class:`Settings` (debounce / grace knobs) and the session id. Optionally
    inject ``spawn`` (defaults to :func:`asyncio.create_task`) so the WS layer
    can route the inference onto the scheduler's shared TaskGroup, and a ``clock``
    for deterministic debounce tests.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        live_state: LiveTranscriptState,
        settings: Settings,
        session_id: str,
        spawn: SpawnTask | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._live_state = live_state
        self._settings = settings
        self._session_id = session_id
        # ``asyncio.create_task`` is generic over the coroutine result; pin it to
        # the loop's ``Coroutine[..., None]`` signature so the attribute type is
        # exactly :data:`SpawnTask` (mypy: a bare assignment would widen it).
        self._spawn: SpawnTask = spawn if spawn is not None else _default_spawn
        self._clock = clock or time.monotonic
        self._debounce_s = max(0.0, settings.THINKER_DEBOUNCE_MS / 1000.0)
        self._grace_s = max(0.0, settings.THINKER_CANCEL_GRACE_MS / 1000.0)

        # Per-turn state. ``_turn_id`` is the FSM turn the loop is armed for;
        # ``_seq`` is the monotonic snapshot counter (anti-stale watermark on the
        # store side). ``_inflight`` is the single in-flight inference task (the
        # ≤1 invariant); ``_pending_text`` / ``_rerun`` carry the newest partial
        # seen while a pass runs so the loop re-evaluates exactly once on
        # completion. ``_last_trigger`` is the monotonic time of the last
        # accepted trigger (debounce). ``_stopped`` latches the cooperative
        # cancel.
        self._turn_id: str | None = None
        self._seq = 0
        self._inflight: asyncio.Task[None] | None = None
        self._pending_text: str | None = None
        self._rerun = False
        self._last_trigger: float | None = None
        self._stopped = False

    # -- public API ----------------------------------------------------------

    def start(self, turn_id: str) -> None:
        """Arm the loop for ``turn_id`` (Annexe B ``start_thinker`` action).

        Resets the per-turn cadence state and CLEARS the store so the new turn
        never inherits the previous turn's snapshot. Synchronous + cheap — called
        from the FSM's ``idle -> user_speaking`` edge. Re-arming with the same id
        (a barge-in ``start_thinker`` restart on the resumed turn) just refreshes
        the cadence so the next partial triggers a fresh pass.
        """

        self._turn_id = turn_id
        self._seq = 0
        self._pending_text = None
        self._rerun = False
        self._last_trigger = None
        self._stopped = False
        self._live_state.clear()

    async def feed_partial(self, partial_text: str) -> None:
        """Handle one ``stt_partial`` — schedule a debounced pass (Annexe H).

        Coalescing rules:

        - If the loop is not armed (no ``start``) or has been stopped, drop it.
        - If a pass is already in flight, record the newest text + flag a single
          rerun (the ≤1-in-flight invariant) and return — the in-flight pass
          re-evaluates against this text when it finishes.
        - Otherwise apply the debounce: a partial within ``THINKER_DEBOUNCE_MS``
          of the last accepted trigger only updates the latest text (so a burst
          of partials coalesces to one inference); past the window we accept the
          trigger and spawn the pass.
        """

        if self._turn_id is None or self._stopped:
            return
        text = partial_text.strip()
        if not text:
            return

        # A pass is running — remember the newest text, schedule exactly one
        # rerun, and let the running pass pick it up on completion.
        if self._inflight is not None and not self._inflight.done():
            self._pending_text = text
            self._rerun = True
            return

        now = self._clock()
        if self._last_trigger is not None and (now - self._last_trigger) < self._debounce_s:
            # Within the debounce window: coalesce — keep the latest text but do
            # not start a new inference. The next out-of-window partial fires it.
            self._pending_text = text
            return

        self._last_trigger = now
        self._pending_text = None
        self._launch(text)

    async def stop(self) -> None:
        """Cooperatively cancel the loop (``endpoint`` / ``bargein`` / ``voice_stop``).

        Annexe H ladder, mirroring the sub-agent scheduler: latch the stop flag
        (so no new pass starts and the in-flight pass will not reschedule), give
        the in-flight inference ``THINKER_CANCEL_GRACE_MS`` to unwind on its own,
        then escalate to :meth:`asyncio.Task.cancel`. Idempotent. Does NOT clear
        the store — the snapshot the Speaker consults at the endpoint must survive
        the freeze (the next :meth:`start` clears it).
        """

        self._stopped = True
        self._rerun = False
        task = self._inflight
        self._inflight = None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(_swallow(task)), timeout=self._grace_s)
        except TimeoutError:
            _logger.warning(
                "thinker_loop.cancel_grace_elapsed",
                session_id=self._session_id,
                turn_id=self._turn_id,
                grace_seconds=self._grace_s,
            )
            task.cancel()
            await _swallow(task)

    async def join(self) -> None:
        """Await the in-flight pass, if any (test / shutdown helper). No cancel."""

        task = self._inflight
        if task is not None and not task.done():
            await _swallow(task)

    @property
    def inflight(self) -> bool:
        """Whether an inference is currently in flight (observability / tests)."""

        return self._inflight is not None and not self._inflight.done()

    # -- internals -----------------------------------------------------------

    def _launch(self, text: str) -> None:
        """Spawn the single in-flight inference task for ``text``."""

        self._inflight = self._spawn(self._run_pass(text))

    async def _run_pass(self, text: str) -> None:
        """Run one Thinker inference over ``text`` → snapshot → store + event.

        Never raises (cooperative cancel aside): a failed inference is logged and
        dropped — a missing Thinker snapshot degrades to the rest of the bounded
        prompt, it must never crash the turn. On completion, if a newer partial
        arrived while we ran (``_rerun``), re-evaluate exactly once against it so
        the model always converges on the latest transcript.
        """

        turn_id = self._turn_id
        try:
            if turn_id is None or self._stopped:
                return
            snapshot = await self._infer(turn_id, text)
            if snapshot is None or self._stopped:
                return
            accepted = self._live_state.update(snapshot)
            if accepted:
                await self._emit_snapshot(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "thinker_loop.pass_failed", session_id=self._session_id, turn_id=turn_id
            )
        finally:
            await self._maybe_rerun()

    async def _maybe_rerun(self) -> None:
        """Re-evaluate once if a partial arrived mid-pass (the ≤1 rerun, Annexe H)."""

        if self._stopped or not self._rerun:
            return
        self._rerun = False
        next_text = self._pending_text
        self._pending_text = None
        if next_text:
            self._last_trigger = self._clock()
            self._launch(next_text)

    async def _infer(self, turn_id: str, text: str) -> ThinkerSnapshot | None:
        """Call the mini model on the partial transcript → a :class:`ThinkerSnapshot`.

        Mints the next monotonic ``seq`` BEFORE the call so a snapshot's ``seq``
        reflects trigger order, then asks the ``thinker`` client for the JSON
        snapshot (guided-gated when the backend supports it). A malformed /
        empty reply yields ``None`` (dropped — no stale snapshot leaks).
        """

        self._seq += 1
        seq = self._seq
        messages = [
            {"role": "system", "content": _THINKER_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        schema = thinker_snapshot_response_schema() if self._client.supports_guided_json() else None
        raw = await self._client.chat(messages, schema=schema, session_id=self._session_id)
        parsed = _parse_snapshot_json(raw)
        if parsed is None:
            return None
        return ThinkerSnapshot(
            turn_id=turn_id,
            seq=seq,
            # An empty ``corrected_text`` from the model falls back to the raw
            # partial so the snapshot always carries SOMETHING to project.
            corrected_text=parsed.corrected_text or text,
            variables=parsed.variables,
            next_step_plan=parsed.next_step_plan,
            user_turn_complete=parsed.user_turn_complete,
            backchannel=parsed.backchannel,
        )

    async def _emit_snapshot(self, snapshot: ThinkerSnapshot) -> None:
        """Emit the Annexe A.2 ``thinker_snapshot`` voice event (scrubbed debug copy).

        Privacy (Annexe A.2): ``corrected_text`` carries user content, so the full
        payload reaches the client while the ``/ws/debug`` ring-buffer copy scrubs
        it to the same leading window as ``stt_*`` (a short fixture survives
        whole). ``user_turn_complete`` is present in the payload (its value is
        EXPLOITED in S7/0103; here it is just carried), and the ``seq`` /
        ``next_step_plan`` / ``variables`` round-trip for the Debug View.
        """

        payload = {
            "type": "thinker_snapshot",
            "turn_id": snapshot.turn_id,
            "seq": snapshot.seq,
            "corrected_text": snapshot.corrected_text,
            "variables": snapshot.variables,
            "next_step_plan": snapshot.next_step_plan,
            "user_turn_complete": snapshot.user_turn_complete,
            "backchannel": snapshot.backchannel,
            "ts": round(self._clock(), 6),
        }
        max_chars = self._settings.STT_DEBUG_TEXT_MAX_CHARS
        debug_payload = {
            **payload,
            "corrected_text": _scrub_text(snapshot.corrected_text, max_chars=max_chars),
        }
        await emit_event(
            payload,
            category="voice",
            severity="debug",
            source="bob.thinker_loop.thinker_snapshot",
            summary=f"thinker_snapshot seq={snapshot.seq} (turn={snapshot.turn_id})",
            debug_payload=debug_payload,
        )


@dataclass(frozen=True)
class _ParsedSnapshot:
    """The model's snapshot reply, defensively coerced to the contract types.

    Each field defaults to its empty value so a wrong-typed / missing key in the
    model's reply degrades to the default rather than crashing the pass.
    """

    corrected_text: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    next_step_plan: str = ""
    user_turn_complete: bool = False
    backchannel: str | None = None


def _parse_snapshot_json(raw: str) -> _ParsedSnapshot | None:
    """Tolerantly decode the model's snapshot reply into a typed dataclass.

    Strips a leading/trailing markdown code fence (some models wrap JSON even when
    told not to), decodes the leading JSON object, and coerces each field to its
    contract type defensively (a wrong-typed field falls back to its default
    rather than crashing the pass). Returns ``None`` when the reply is not a JSON
    object at all — the pass then drops the snapshot (no stale leak).
    """

    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            first = lines[0].lstrip("`").strip().lower()
            if first in ("", "json"):
                end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
                text = "\n".join(lines[1:end]).strip()
    if not text:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    corrected = payload.get("corrected_text")
    variables = payload.get("variables")
    plan = payload.get("next_step_plan")
    complete = payload.get("user_turn_complete")
    backchannel = payload.get("backchannel")
    return _ParsedSnapshot(
        corrected_text=corrected if isinstance(corrected, str) else "",
        variables=variables if isinstance(variables, dict) else {},
        next_step_plan=plan if isinstance(plan, str) else "",
        # ``bool`` is an ``int`` subclass — accept only a real bool, not 0/1.
        user_turn_complete=complete if isinstance(complete, bool) else False,
        backchannel=(backchannel if isinstance(backchannel, str) and backchannel.strip() else None),
    )


def _default_spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Default :data:`SpawnTask` — a bare :func:`asyncio.create_task`.

    Split out (rather than assigning ``asyncio.create_task`` directly) so the
    attribute type is exactly :data:`SpawnTask`; ``asyncio.create_task`` is
    generic over the coroutine result and would widen the inferred type.
    """

    return asyncio.create_task(coro)


async def _swallow(task: asyncio.Task[None]) -> None:
    """Await ``task`` suppressing any exception / cancellation (cleanup helper)."""

    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


__all__ = ["ThinkerLoop", "thinker_snapshot_response_schema"]
