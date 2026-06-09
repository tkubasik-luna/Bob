"""SpeculativeDraft — the « Penser en parallèle » anticipation pass (issue 0104).

PRD 0016 / Annexe A.2 + F + G (the ``SpeculativeDraft`` paragraph + the commit
gate). While the user is still speaking, the full-duplex loop feeds every
``stt_partial`` to a :class:`SpeculativeDraft` running on a MINI fast model (the
``draft`` role, default a small local model — see
:func:`bob.llm.factory.build_draft_role_client`). Each pass pre-writes a RAW,
OFF-CODEC conversational reply (NOT a validated tool-call) to the partial
transcript, so that at the endpoint Bob can answer near-instantly instead of
cold-generating from scratch.

This MIRRORS :class:`bob.thinker_loop.ThinkerLoop` exactly (the background-loop
pattern: debounced, ≤1 inference in flight per turn, cooperative cancel under the
injected ``spawn``), and the two run IN PARALLEL on distinct role clients over
the same partial transcript. The crucial difference: the Thinker maintains a
structured UNDERSTANDING (the snapshot the Speaker consults); the Draft pre-writes
the actual SPOKEN ANSWER (the text the Speaker may adopt verbatim).

What is speculated (and what is not)
------------------------------------

The Draft speculates ONLY the conversational reply. A turn that would dispatch a
tool falls back to COLD (no draft adopted) — the cold path runs the normal
Speaker say-path which can spawn a sub-task / call a tool, something the raw
draft text can never do. To keep that boundary pure + testable the loop accepts
an optional :data:`ToolIntentPredicate` (``is_tool_intent``): when it returns
``True`` for the latest partial the loop produces NO draft, so the endpoint has
nothing to adopt and the turn stays cold. ``None`` (the default) always
speculates the conversational reply — the bare loop wires it off so the cold
path is byte-for-byte unchanged.

The commit gate (Annexe F, normative)
--------------------------------------

At the endpoint the loop calls the PURE :meth:`commit_gate` with the FINAL
frozen transcript. It decides, deterministically, whether the speculative draft
may be adopted:

1. **Prefix fast-path** — the final transcript is (approximately) a prefix-or-
   extension of the partial the draft was built on (the common case: the partial
   the user had spoken when the draft fired settles into the final with only a
   few trailing tokens). Commit instantly.
2. **Light similarity guard** — else a pure token-overlap ratio
   (:func:`_token_overlap`, a Jaccard over the normalised token *sets*) at or
   above ``DRAFT_COMMIT_SIMILARITY`` commits. Kept deliberately simple + pure (no
   embeddings, no I/O) so it is unit-testable and deterministic.
3. **Discard** — else the draft is thrown away and the Speaker regenerates COLD.

The decision is returned as a :class:`DraftDecision` carrying the gate ``state``
(``committed`` | ``discarded``), a ``reason`` and (when committed) the ``text``
to adopt. A turn with no draft at all (the draft never fired, or it was
suppressed as a tool turn, or the draft model was unavailable — Annexe G) returns
a ``discarded`` decision with the matching reason so the loop runs cold.

Degradation (Annexe G — "Draft model indispo → désactive l'anticipation,
toujours froid"): the loop NEVER raises. A failed inference is logged + dropped
(no draft → cold). The WS layer omits the loop entirely when the draft client
cannot be built, so the whole anticipation path is simply off and every turn is
cold — exactly the 0100/0103 behaviour.

Events (Annexe A.2)
-------------------

The loop emits ``draft_status {turn_id, state, reason?, ts}`` (category
``voice``) on each phase: ``drafting`` (a pass started), ``ready`` (a draft
landed), ``committed`` / ``discarded`` (the gate's verdict — emitted by the loop
on behalf of the endpoint). The committed/discarded events are stamped by the
caller via :meth:`emit_decision` so the marks + the event stay in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import structlog

from bob.config import Settings
from bob.event_bus_v2 import emit_event
from bob.llm_client import LLMClient
from bob.voice_turn import _scrub_text

_logger = structlog.get_logger(__name__)

#: Factory the loop uses to spawn its inference coroutine — identical contract to
#: :data:`bob.thinker_loop.SpawnTask`. The WS layer wires the scheduler's shared
#: :class:`asyncio.TaskGroup` here (structured concurrency); tests pass
#: :func:`asyncio.create_task`.
SpawnTask = Callable[[Coroutine[Any, Any, None]], "asyncio.Task[None]"]

#: Pure predicate over the latest partial transcript: does this turn look like it
#: will DISPATCH A TOOL (so it must stay COLD — no draft adopted)? ``True`` ⇒ the
#: loop produces no draft. Injected so the boundary stays testable; ``None`` ⇒
#: always speculate the conversational reply (the bare-loop default).
ToolIntentPredicate = Callable[[str], bool]


#: System prompt for the mini Draft model. It asks for a SHORT, natural spoken
#: reply to the user's (possibly incomplete) transcript — raw text, no tool, no
#: JSON, no preamble. Kept terse: the model is small + latency-critical, and the
#: output is adopted verbatim into the say-path when the gate commits.
_DRAFT_SYSTEM_PROMPT = (
    "Tu es la voix d'un assistant personnel en conversation orale. On te donne "
    "la transcription PARTIELLE de ce que l'utilisateur est en train de dire "
    "(elle peut être incomplète). Rédige par AVANCE la réponse que l'assistant "
    "dira à voix haute si la phrase se termine ainsi. Réponds UNIQUEMENT par le "
    "texte parlé, court et naturel, sans préambule, sans guillemets, sans "
    "formatage."
)


@dataclass(frozen=True)
class DraftDecision:
    """The commit gate's verdict for one endpoint (Annexe A.2 ``draft_status``).

    - ``state`` — ``"committed"`` (the draft may be adopted into the say-path) or
      ``"discarded"`` (regenerate COLD).
    - ``reason`` — a short machine tag for *why* (``"prefix"`` / ``"similarity"``
      / ``"divergence"`` / ``"no_draft"`` / ``"tool_turn"`` / ``"draft_unavailable"``).
    - ``text`` — the spoken text to adopt when committed (empty on discard).
    - ``similarity`` — the measured token-overlap ratio (for observability /
      tests); ``None`` when the gate short-circuited before computing it.
    """

    state: str
    reason: str
    text: str = ""
    similarity: float | None = None

    @property
    def committed(self) -> bool:
        return self.state == "committed"


def _normalise(text: str) -> str:
    """Collapse whitespace + casefold — the canonical form for the gate compares."""

    return " ".join(text.split()).casefold()


def _tokens(text: str) -> list[str]:
    """Alphanumeric word tokens of ``text`` (punctuation dropped), normalised.

    Splits on any non-alphanumeric run (so apostrophes, hyphens, commas and the
    like never glue onto a token) over the casefolded text — robust to the
    punctuation a draft reply or a settled transcript may carry while a raw STT
    partial does not.
    """

    return [tok for tok in re.split(r"[^0-9a-zÀ-ɏ]+", _normalise(text)) if tok]


def _is_prefix_match(partial: str, final: str) -> bool:
    """Whether ``final`` is (approximately) a prefix-or-extension of ``partial``.

    The fast-path case: the partial the draft fired on settles into the final
    with only trailing words added (or removed) — the user finished the clause he
    had started, so the pre-written reply still answers it. We treat it as a match
    when, on the normalised token streams, the SHORTER is a leading run of the
    LONGER (one is a prefix of the other). Empty inputs never match (nothing was
    said / drafted).
    """

    a = _tokens(partial)
    b = _tokens(final)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def _token_overlap(partial: str, final: str) -> float:
    """Pure token-set Jaccard overlap of two transcripts (0..1).

    A deliberately simple, embedding-free similarity: ``|A inter B| / |A union B|``
    over the normalised token SETS. Robust to word-order + duplicate-word noise, which
    is enough to tell "the user said essentially the same thing the draft was
    built on" from "the user diverged". Two empty inputs ⇒ 0.0 (nothing to
    compare — the gate treats it as no match).
    """

    a = set(_tokens(partial))
    b = set(_tokens(final))
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


class SpeculativeDraft:
    """Per-session speculative drafter — debounced, ≤1 inference in flight per turn.

    Construct with the ``draft`` role :class:`LLMClient` (mini fast model), the
    resolved :class:`Settings` (debounce / grace / similarity knobs) and the
    session id. Optionally inject ``spawn`` (defaults to
    :func:`asyncio.create_task`) so the WS layer can route the inference onto the
    scheduler's shared TaskGroup, an ``is_tool_intent`` predicate (tool turns stay
    cold), and a ``clock`` for deterministic debounce tests.

    Lifecycle mirrors :class:`bob.thinker_loop.ThinkerLoop`: :meth:`start` arms
    the loop for a turn, :meth:`feed_partial` is the per-``stt_partial`` hook, and
    :meth:`stop` cooperatively cancels at ``endpoint`` / ``bargein`` /
    ``voice_stop`` (cancel + grace + hard-kill). The latest landed draft survives
    the freeze so :meth:`commit_gate` can read it at the endpoint (the next
    :meth:`start` clears it).
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        settings: Settings,
        session_id: str,
        spawn: SpawnTask | None = None,
        is_tool_intent: ToolIntentPredicate | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._session_id = session_id
        self._spawn: SpawnTask = spawn if spawn is not None else _default_spawn
        self._is_tool_intent = is_tool_intent
        self._clock = clock or time.monotonic
        # Reuse the Thinker cadence knobs (the two loops share the « Penser en
        # parallèle » debounce/grace contract — Annexe H). The cooperative grace
        # is CAPPED (PRD 0018 / issue 0118): the endpoint freeze sits on the
        # user-audible critical path, so a parked inference gets at most
        # ``THINKER_CANCEL_GRACE_CAP_MS`` before the hard cancel.
        self._debounce_s = max(0.0, settings.THINKER_DEBOUNCE_MS / 1000.0)
        self._grace_s = min(
            max(0.0, settings.THINKER_CANCEL_GRACE_MS / 1000.0),
            max(0.0, settings.THINKER_CANCEL_GRACE_CAP_MS / 1000.0),
        )
        self._similarity_threshold = max(0.0, min(1.0, settings.DRAFT_COMMIT_SIMILARITY))

        # Per-turn state (same shape as the Thinker loop's).
        self._turn_id: str | None = None
        self._inflight: asyncio.Task[None] | None = None
        self._pending_text: str | None = None
        self._rerun = False
        self._last_trigger: float | None = None
        self._stopped = False
        # The latest landed draft + the partial it was built on. ``None`` until a
        # pass produces one; survives :meth:`stop` so the endpoint gate reads it.
        self._draft_text: str | None = None
        self._draft_partial: str = ""
        # Whether the latest partial was classified a TOOL turn (no draft). The
        # gate uses this to report ``reason="tool_turn"`` rather than ``no_draft``.
        self._tool_turn = False

    # -- public API ----------------------------------------------------------

    def start(self, turn_id: str) -> None:
        """Arm the loop for ``turn_id`` (mirrors ThinkerLoop.start).

        Resets the per-turn cadence + clears the held draft so the new turn never
        adopts the previous turn's pre-written reply. Synchronous + cheap — called
        from the FSM's ``idle -> user_speaking`` edge. Re-arming with the same id
        (a barge-in restart) just refreshes the cadence.
        """

        self._turn_id = turn_id
        self._pending_text = None
        self._rerun = False
        self._last_trigger = None
        self._stopped = False
        self._draft_text = None
        self._draft_partial = ""
        self._tool_turn = False

    async def feed_partial(self, partial_text: str) -> None:
        """Handle one ``stt_partial`` — schedule a debounced draft pass (Annexe H).

        Coalescing rules identical to :meth:`bob.thinker_loop.ThinkerLoop.feed_partial`:
        drop when unarmed / stopped; if a pass is in flight record the newest text
        + flag a single rerun; else apply the debounce (a partial within
        ``THINKER_DEBOUNCE_MS`` of the last accepted trigger only updates the
        latest text). A turn classified as a TOOL turn by ``is_tool_intent``
        produces NO draft (it is recorded so the gate reports ``tool_turn``).
        """

        if self._turn_id is None or self._stopped:
            return
        text = partial_text.strip()
        if not text:
            return

        # Tool turns stay cold: never draft a conversational reply for a turn that
        # will dispatch a tool (the cold say-path handles those). Record it so the
        # endpoint gate reports ``reason="tool_turn"`` rather than ``no_draft``.
        if self._is_tool_intent is not None:
            with contextlib.suppress(Exception):
                if self._is_tool_intent(text):
                    self._tool_turn = True
                    return
        self._tool_turn = False

        if self._inflight is not None and not self._inflight.done():
            self._pending_text = text
            self._rerun = True
            return

        now = self._clock()
        if self._last_trigger is not None and (now - self._last_trigger) < self._debounce_s:
            self._pending_text = text
            return

        self._last_trigger = now
        self._pending_text = None
        self._launch(text)

    async def stop(self) -> None:
        """Cooperatively cancel the loop (mirrors ThinkerLoop.stop).

        Latch the stop flag, give the in-flight inference the capped grace
        (``min(THINKER_CANCEL_GRACE_MS, THINKER_CANCEL_GRACE_CAP_MS)`` — PRD
        0018 / issue 0118) to unwind, then escalate to
        :meth:`asyncio.Task.cancel`. Idempotent. Does NOT clear the held draft —
        the endpoint gate must read the pre-written reply AFTER the freeze (the
        next :meth:`start` clears it).
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
                "speculative_draft.cancel_grace_elapsed",
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
        """Whether a draft inference is currently in flight (observability/tests)."""

        return self._inflight is not None and not self._inflight.done()

    @property
    def draft_text(self) -> str | None:
        """The latest landed draft reply (``None`` if none) — observability/tests."""

        return self._draft_text

    def commit_gate(self, final_transcript: str) -> DraftDecision:
        """Decide whether the speculative draft may be adopted (PURE, Annexe F).

        The deterministic gate the endpoint runs on the FINAL frozen transcript:

        1. No draft → ``discarded`` (``reason`` ``tool_turn`` when the turn was a
           tool turn, else ``no_draft``).
        2. **Prefix fast-path** — the final is ~a prefix-or-extension of the
           partial the draft fired on → ``committed`` (``reason="prefix"``).
        3. **Similarity guard** — token-overlap ≥ ``DRAFT_COMMIT_SIMILARITY`` →
           ``committed`` (``reason="similarity"``).
        4. Else → ``discarded`` (``reason="divergence"``), carrying the measured
           ratio so a red verdict is self-explanatory.

        Pure: no I/O, no time, no mutation — every branch is independently
        unit-testable. The held draft is read but not cleared (the loop clears on
        the next :meth:`start`).
        """

        draft = self._draft_text
        if draft is None or not draft.strip():
            reason = "tool_turn" if self._tool_turn else "no_draft"
            return DraftDecision(state="discarded", reason=reason)

        final = final_transcript or ""
        if _is_prefix_match(self._draft_partial, final):
            return DraftDecision(state="committed", reason="prefix", text=draft, similarity=1.0)

        similarity = _token_overlap(self._draft_partial, final)
        if similarity >= self._similarity_threshold:
            return DraftDecision(
                state="committed", reason="similarity", text=draft, similarity=similarity
            )
        return DraftDecision(state="discarded", reason="divergence", similarity=similarity)

    async def emit_decision(self, turn_id: str, decision: DraftDecision) -> None:
        """Emit the terminal ``draft_status`` event for the gate verdict (Annexe A.2).

        Called by the endpoint (the full-duplex loop) after :meth:`commit_gate` so
        the marks (``t_commit_decision`` / ``draft_hit``) and the wire event are
        stamped together. ``state`` is ``committed`` / ``discarded``; ``reason`` is
        always present.
        """

        await self._emit_status(turn_id, decision.state, reason=decision.reason)

    # -- internals -----------------------------------------------------------

    def _launch(self, text: str) -> None:
        self._inflight = self._spawn(self._run_pass(text))

    async def _run_pass(self, text: str) -> None:
        """Run one Draft inference over ``text`` → a raw reply → hold it + event.

        Never raises (cooperative cancel aside): a failed inference is logged +
        dropped (no draft → the endpoint stays cold, Annexe G). Emits ``drafting``
        when the pass starts and ``ready`` when a non-empty reply lands. On
        completion re-evaluates exactly once if a newer partial arrived mid-pass
        (the ≤1 rerun).
        """

        turn_id = self._turn_id
        try:
            if turn_id is None or self._stopped:
                return
            await self._emit_status(turn_id, "drafting")
            reply = await self._infer(text)
            if self._stopped or turn_id != self._turn_id:
                return
            if reply and reply.strip():
                self._draft_text = reply.strip()
                self._draft_partial = text
                await self._emit_status(turn_id, "ready")
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "speculative_draft.pass_failed", session_id=self._session_id, turn_id=turn_id
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

    async def _infer(self, text: str) -> str:
        """Call the mini Draft model on the partial transcript → raw reply text.

        Off-codec by design: a plain ``chat`` call (no tools, no schema) so the
        reply is the spoken text itself, not a tool-call envelope. The orchestrator
        is NOT involved — adopting the result later is a trivial text validation.
        """

        messages = [
            {"role": "system", "content": _DRAFT_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        return await self._client.chat(messages, session_id=self._session_id)

    async def _emit_status(self, turn_id: str, state: str, *, reason: str | None = None) -> None:
        """Emit the Annexe A.2 ``draft_status`` voice event (scrubbed debug copy).

        Payload (Annexe A.2): ``{turn_id, state, reason?, ts}`` (category
        ``voice``). The ``draft`` text itself is NEVER on the wire here (privacy +
        it is the unspoken speculation); only the lifecycle state travels. The
        ``/ws/debug`` ring-buffer copy carries a scrubbed leading window of the
        held draft under ``draft_preview`` purely for the Debug View, capped like
        every other user-derived text field.
        """

        payload: dict[str, Any] = {
            "type": "draft_status",
            "turn_id": turn_id,
            "state": state,
            "ts": round(self._clock(), 6),
        }
        if reason is not None:
            payload["reason"] = reason
        debug_payload = {
            **payload,
            "draft_preview": _scrub_text(
                self._draft_text or "", max_chars=self._settings.STT_DEBUG_TEXT_MAX_CHARS
            ),
        }
        await emit_event(
            payload,
            category="voice",
            severity="debug",
            source="bob.speculative_draft.draft_status",
            summary=f"draft_status {state} (turn={turn_id})",
            debug_payload=debug_payload,
        )


def _default_spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Default :data:`SpawnTask` — a bare :func:`asyncio.create_task`.

    Split out (rather than assigning ``asyncio.create_task`` directly) so the
    attribute type is exactly :data:`SpawnTask` (mirrors the Thinker loop).
    """

    return asyncio.create_task(coro)


async def _swallow(task: asyncio.Task[None]) -> None:
    """Await ``task`` suppressing any exception / cancellation (cleanup helper)."""

    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


__all__ = ["DraftDecision", "SpeculativeDraft", "ToolIntentPredicate"]
