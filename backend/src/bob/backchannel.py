"""Backchannel decision — when to drop a brief "mm" / "ok je vois" in a pause.

PRD 0016 / issue 0105 (« Backchannels » + Annexe B + A.2 + F). A *backchannel* is
a short acknowledgement Bob places **in a pause of the user's speech** to keep the
exchange alive — never over active speech, never a floor transition. Annexe B is
explicit: it is an **action** emitted on the ``user_speaking --vad_pause-->
user_speaking [maybe_backchannel]`` self-loop, NOT a state (so there is no overlap
state and the turn stays in ``user_speaking``).

This module owns ONLY the *decision* (pure, synchronous, fully unit-testable);
the effects — synthesising the token via Kokoro, emitting the ``backchannel``
event, stamping ``backchannel_ms`` — live in :mod:`bob.voice_loop`, exactly the
producer/effect split :class:`bob.turn_fsm.TurnFsm` has against the loop. The loop
calls :meth:`BackchannelDecider.decide` on every ``vad_pause`` while the user
holds the floor and performs the synthesis only when it returns a token.

The proactivity gate (mirrors ``proactivity_handler``)
------------------------------------------------------

A backchannel must NOT fire on every pause (that would machine-gun "mm, ok, mm,
ok"). It is gated like the existing inner-thoughts "when-to-speak" proactivity
(:mod:`bob.proactivity_handler` gates a proactive push on a *condition* — user
idleness — rather than emitting on every trigger). Here the gate has two parts,
both required:

1. **Relevance** — the background Thinker must carry a non-empty ``backchannel``
   trigger on its latest snapshot (:attr:`bob.live_transcript_state.ThinkerSnapshot.backchannel`).
   That string IS the relevance signal: the mini reasoning pass decided an
   acknowledgement is warranted for what the user is saying. No token ⇒ no
   backchannel (a pause the Thinker did not flag stays silent).
2. **Silence-decay (refractory)** — after Bob places a backchannel, his
   proactivity budget is spent and **decays back over time**: a second
   backchannel within :attr:`min_interval_s` of the last one is suppressed, so
   pauses that come in a burst yield at most one acknowledgement. Once enough
   wall-clock has elapsed (the budget recovered) a fresh relevant trigger is
   allowed again. This is the decay term — the temporal analogue of the
   proactivity handler's idleness gate.

Keeping the decision pure (no clock of its own, no I/O) means the loop owns the
``now`` (its monotone server clock) and the decider just compares it to the last
emission — so every branch (no token, token + cold, token + within refractory,
token + recovered) is testable without a running loop or a TTS engine.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Default refractory window (seconds): the minimum gap between two backchannels
#: on one turn. Mirrors the spirit of ``proactivity_handler``'s idleness gate —
#: Bob's proactivity budget decays back over ~this window so pauses in a burst
#: yield at most one acknowledgement. The loop wires it from
#: ``BACKCHANNEL_MIN_INTERVAL_MS`` (config) so nothing is hard-coded on the path.
DEFAULT_MIN_INTERVAL_S: float = 1.5


@dataclass(frozen=True)
class BackchannelDecision:
    """The outcome of one :meth:`BackchannelDecider.decide` call.

    - ``emit`` — whether the loop should synthesise + play a backchannel now.
    - ``token`` — the short acknowledgement text to speak (``""`` when not
      emitting). It is the Thinker's trigger string verbatim, capped to a short
      phrase so a runaway model reply can never turn a backchannel into a full
      utterance (Annexe B: a backchannel is brief by construction).
    - ``reason`` — why the gate decided as it did (``"no_trigger"`` /
      ``"refractory"`` / ``"emit"``), echoed into the loop's debug log so a
      suppressed backchannel is explainable, never silent.
    """

    emit: bool
    token: str = ""
    reason: str = "no_trigger"


@dataclass
class BackchannelDecider:
    """Pure proactivity gate for pause backchannels (issue 0105).

    One instance per WS session (the loop holds it). :meth:`decide` is called on
    every ``vad_pause`` while the user holds the floor with the Thinker's latest
    ``backchannel`` trigger + the loop's ``now``; it returns a
    :class:`BackchannelDecision`. On an emitted decision the loop calls
    :meth:`note_emitted` with the same ``now`` so the refractory window starts —
    keeping the "last emission" bookkeeping inside the decider rather than the
    loop. :meth:`reset` clears it at a turn boundary so the next turn starts with
    a fresh proactivity budget (no carry-over from a previous turn's last
    backchannel).
    """

    #: Refractory window in seconds (silence-decay term). A second backchannel
    #: within this gap of the last one is suppressed.
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    #: Hard cap on the spoken token length (characters). A backchannel is brief
    #: by construction (Annexe B); a longer Thinker string is truncated so it can
    #: never become a full utterance over the user's pause.
    max_token_chars: int = 24

    #: Monotone timestamp of the last emitted backchannel this turn (``None`` =
    #: none yet, so the first relevant trigger is always cold-allowed).
    _last_emit: float | None = None

    def decide(self, *, trigger: str | None, now: float) -> BackchannelDecision:
        """Decide whether to place a backchannel in the current pause.

        ``trigger`` is the Thinker's latest ``backchannel`` token (``None`` /
        blank when the Thinker has nothing to interject). ``now`` is the loop's
        monotone clock at the pause. The gate (both required):

        1. relevance — a non-empty ``trigger`` (the Thinker flagged it);
        2. silence-decay — the last backchannel was ≥ ``min_interval_s`` ago (or
           there was none this turn).

        Returns an ``emit=True`` decision carrying the (capped) token only when
        BOTH hold; otherwise ``emit=False`` with the reason. Pure — it neither
        mutates state nor reads a clock; the loop calls :meth:`note_emitted` to
        arm the refractory window after it actually plays the token.
        """

        raw = (trigger or "").strip()
        if not raw:
            return BackchannelDecision(emit=False, reason="no_trigger")
        token = raw[: self.max_token_chars]

        # Silence-decay refractory: suppress a second backchannel that lands
        # within ``min_interval_s`` of the last one (the proactivity budget has
        # not recovered yet). ``min_interval_s <= 0`` disables the term (every
        # relevant pause is allowed — used by tests / an "always" config).
        if (
            self._last_emit is not None
            and self.min_interval_s > 0
            and (now - self._last_emit) < self.min_interval_s
        ):
            return BackchannelDecision(emit=False, token=token, reason="refractory")

        return BackchannelDecision(emit=True, token=token, reason="emit")

    def note_emitted(self, now: float) -> None:
        """Arm the refractory window after a backchannel was actually played.

        The loop calls this with the SAME ``now`` it passed to :meth:`decide`
        once the synthesis + event have been issued, so a synthesis that the loop
        skipped (e.g. an empty token, an engine miss) never spends the budget.
        """

        self._last_emit = now

    def reset(self) -> None:
        """Clear the per-turn proactivity budget (a turn boundary / new turn).

        Drops the last-emission watermark so the next turn's first relevant
        trigger is cold-allowed (no carry-over). Called by the loop when it opens
        a fresh turn, mirroring the VAD / Endpointer per-turn resets.
        """

        self._last_emit = None


__all__ = ["DEFAULT_MIN_INTERVAL_S", "BackchannelDecider", "BackchannelDecision"]
