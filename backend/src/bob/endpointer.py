"""End-of-turn detection — silence floor (0100) + semantic endpoint (0103).

PRD 0016 / Annexe B: the ``endpoint`` event drives the
:class:`bob.turn_fsm.TurnFsm` from ``user_speaking`` → ``thinking`` (freeze the
transcript, request a reply). The PRD's full endpointer is **hybrid** — a VAD
silence floor *plus* a semantic ``user_turn_complete`` signal from the Thinker
(:class:`bob.thinker_loop.ThinkerLoop`). Issue 0100 built the deterministic half
(the silence floor); issue 0103 (this module) adds the semantic half and merges
the two sources.

The two sources (issue 0103 + Annexe B/H)
-----------------------------------------

1. **VAD silence floor** (issue 0100, the net): count how long the stream has
   been continuously silent since the user last spoke and declare ``endpoint``
   once that run reaches ``silence_floor_frames``. This always fires eventually
   when the user stops — even if no semantic signal ever arrives.
2. **Semantic ``user_turn_complete``** (issue 0103): the background Thinker
   reads the partial transcript and decides whether the user's clause *means*
   it is finished. When it is, the turn can end EARLIER than the silence floor
   — but a raw ``user_turn_complete`` is treated as a *hypothesis*, never an
   immediate trigger, because a mini model can call it complete mid-sentence.

Anti-false-positive — the confirmation rule (Annexe H, normative)
-----------------------------------------------------------------

``user_turn_complete`` fires the endpoint ONLY when **confirmed by the next
stable partial**. Concretely: when the Thinker signals completeness we *arm* a
pending semantic endpoint and snapshot the STT's current ``stable_prefix_len``
(the number of leading transcript characters whisper considers settled — see
:class:`bob.stt_engine.SttPartial`). The pending endpoint then fires only once a
*later* partial reports a ``stable_prefix_len`` that has **advanced past** that
watermark — i.e. the hypothesis settled further, proving the user really
stopped adding words at that clause rather than pausing mid-thought. If instead

- the user resumes speaking (a speech frame), or
- the Thinker withdraws the signal (a fresh ``user_turn_complete=false``),

the pending semantic endpoint is disarmed and the **silence floor remains the
net**. So a mid-sentence hesitation never ends the turn early, while a genuinely
complete clause ends it before the (longer) silence floor would.

Both halves observe the same per-frame speech/silence decision; the semantic
half additionally consumes the Thinker signal + the STT stable prefix the loop
routes in. The detector arms (for the floor) only after speech has been
observed, and it fires its ``endpoint`` exactly once per turn regardless of
which source crossed first. Reset per turn.

Frame-driven + pure: feed it the same ``is_speech`` decision the VAD computes
(or let it compute its own from the PCM); feed it the Thinker signal via
:meth:`note_user_turn_complete` and the STT stable prefix via
:meth:`note_stable_prefix`; it returns ``True`` on the single frame that crosses
*either* source. No time, no I/O — the loop owns the clock and the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from bob.vad import rms_normalised


@dataclass
class Endpointer:
    """Hybrid end-of-turn detector — silence floor (0100) + semantic (0103).

    Construct with the floor expressed as a frame count
    (:attr:`silence_floor_frames`) — the WS layer derives it from a
    millisecond setting via :meth:`bob.vad.EnergyVad.frames_for_ms` so the
    tunable stays human-readable. Then drive it per frame:

    - :meth:`observe` with a precomputed ``is_speech`` bool (when the caller
      already ran the VAD on the frame — the production path, no double RMS), or
    - :meth:`feed_frame` with the raw PCM payload (it computes ``is_speech``
      itself from :attr:`speech_rms` — handy for isolation tests).

    For the SEMANTIC source (issue 0103), additionally route in, as they arrive:

    - :meth:`note_user_turn_complete` — the Thinker's ``user_turn_complete``
      from its latest snapshot (arms / disarms the pending semantic endpoint);
    - :meth:`note_stable_prefix` — the STT partial's ``stable_prefix_len`` (the
      confirmation signal — an advance past the arm-time watermark confirms).

    ``observe`` / ``feed_frame`` return ``True`` on the frame that crosses
    EITHER source (the ``endpoint``), ``False`` otherwise. The floor arms only
    after the first speech frame; the detector fires at most once until
    :meth:`reset`.
    """

    #: Consecutive silent frames (after speech) that constitute a finished turn.
    silence_floor_frames: int = 20
    #: RMS threshold used only by :meth:`feed_frame` (the self-deciding path).
    speech_rms: float = 0.02

    _armed: bool = False
    _fired: bool = False
    _silence_run: int = 0

    # -- semantic-endpoint state (issue 0103) --------------------------------
    #: True between a ``user_turn_complete=true`` signal and its confirmation /
    #: disarm: the loop has armed a pending semantic endpoint and is waiting for
    #: the next stable partial to confirm it (Annexe H).
    _semantic_pending: bool = False
    #: The ``stable_prefix_len`` watermark captured when the semantic endpoint
    #: was armed. A later partial whose stable prefix exceeds this confirms the
    #: clause settled (the anti-false-positive gate). ``None`` while disarmed.
    _semantic_armed_stable: int | None = None
    #: Latest ``stable_prefix_len`` observed this turn (so a signal that arrives
    #: AFTER the confirming partial still has the right watermark to compare).
    _latest_stable: int = 0
    #: Set once the confirmation rule is satisfied; the next :meth:`observe`
    #: fires the endpoint from the semantic source. Latches with ``_fired``.
    _semantic_confirmed: bool = False

    def __post_init__(self) -> None:
        if self.silence_floor_frames < 1:
            self.silence_floor_frames = 1

    @property
    def armed(self) -> bool:
        """True once speech has been seen (the silence floor can now fire)."""

        return self._armed

    @property
    def semantic_pending(self) -> bool:
        """True while a semantic endpoint is armed but not yet confirmed (tests)."""

        return self._semantic_pending

    def note_user_turn_complete(self, complete: bool) -> None:
        """Route the Thinker's ``user_turn_complete`` signal (issue 0103).

        ``True`` ARMS a pending semantic endpoint, snapshotting the current STT
        stable-prefix watermark so the next stable partial can confirm it
        (Annexe H). Re-arming is idempotent — it keeps the earliest watermark so
        a partial that already advanced still confirms. ``False`` (the Thinker
        no longer thinks the clause is done) DISARMS the pending endpoint and any
        confirmation it had latched, so the silence floor is back to being the
        net. No-op once the detector has already fired this turn.
        """

        if self._fired:
            return
        if complete:
            if not self._semantic_pending:
                self._semantic_pending = True
                self._semantic_armed_stable = self._latest_stable
                # A stable partial may already have advanced past the watermark
                # by the time the (async) signal lands — treat that as immediate
                # confirmation so a late-but-valid signal is not stranded.
                self._reevaluate_confirmation()
        else:
            self._disarm_semantic()

    def note_stable_prefix(self, stable_prefix_len: int) -> None:
        """Route the STT partial's ``stable_prefix_len`` — the confirmation (0103).

        Records the latest stable-prefix length and, if a semantic endpoint is
        pending, checks the confirmation rule: a stable prefix that has ADVANCED
        past the watermark captured at arm-time confirms the clause settled
        (Annexe H). Once confirmed, the next :meth:`observe` fires the endpoint.
        A non-advancing partial (the hypothesis still churning) does not confirm
        — Bob keeps waiting. No-op once fired.
        """

        if self._fired:
            return
        # Stable prefix only grows within a turn; guard against a spurious
        # regression so the watermark comparison stays monotone.
        if stable_prefix_len > self._latest_stable:
            self._latest_stable = stable_prefix_len
        self._reevaluate_confirmation()

    def observe(self, *, is_speech: bool) -> bool:
        """Advance the detector one frame; return ``True`` on the ``endpoint``.

        Fires on the frame that first crosses EITHER source:

        - the **semantic** endpoint, once :meth:`note_user_turn_complete` armed
          it AND :meth:`note_stable_prefix` confirmed it (the Annexe H rule); or
        - the **silence floor**, once the trailing silence run (after speech)
          reaches :attr:`silence_floor_frames` (the net).

        A speech frame arms the floor, clears the silence run, AND disarms any
        pending/confirmed semantic endpoint — the user resumed, so the clause is
        no longer complete. Once fired the detector is latched until
        :meth:`reset`.
        """

        if self._fired:
            return False
        if is_speech:
            self._armed = True
            self._silence_run = 0
            # A speech frame drops an UNCONFIRMED pending semantic endpoint: the
            # user is still mid-clause, so a ``user_turn_complete`` the Thinker
            # flagged before the stable prefix settled was a false positive
            # (Annexe H anti-false-positive). A CONFIRMED endpoint survives — the
            # stable prefix already settled, i.e. the clause genuinely finished;
            # if the user truly resumes a new clause the Thinker withdraws the
            # signal (a later ``user_turn_complete=false``) and the silence floor
            # is back to being the net.
            if not self._semantic_confirmed:
                self._disarm_semantic()
            return False

        # Nothing ends a turn that never had speech (the floor's invariant,
        # shared by the semantic source): a confirmed clause still needs the turn
        # to have actually started.
        if not self._armed:
            return False

        # Semantic source: a confirmed complete clause ends the turn EARLIER than
        # the silence floor (it does not require the floor's silence run, only
        # that the user is not currently speaking — checked above — and that
        # speech was observed this turn — checked just above).
        if self._semantic_confirmed:
            self._fired = True
            return True

        # Silence floor (the net).
        self._silence_run += 1
        if self._silence_run >= self.silence_floor_frames:
            self._fired = True
            return True
        return False

    def feed_frame(self, pcm: bytes) -> bool:
        """Compute ``is_speech`` from raw PCM, then :meth:`observe` it."""

        return self.observe(is_speech=rms_normalised(pcm) >= self.speech_rms)

    def reset(self) -> None:
        """Reset for a fresh turn (disarm + clear both sources + un-fire)."""

        self._armed = False
        self._fired = False
        self._silence_run = 0
        self._latest_stable = 0
        self._disarm_semantic()

    # -- internals -----------------------------------------------------------

    def _reevaluate_confirmation(self) -> None:
        """Confirm the pending semantic endpoint iff the stable prefix advanced.

        The anti-false-positive gate (Annexe H): a stable prefix that has grown
        past the watermark captured when ``user_turn_complete`` armed the
        endpoint confirms the clause settled. Idempotent — once confirmed it
        stays confirmed until a resume / disarm clears it.
        """

        if not self._semantic_pending or self._semantic_armed_stable is None:
            return
        if self._latest_stable > self._semantic_armed_stable:
            self._semantic_confirmed = True

    def _disarm_semantic(self) -> None:
        """Drop the pending/confirmed semantic endpoint (resume / withdraw)."""

        self._semantic_pending = False
        self._semantic_armed_stable = None
        self._semantic_confirmed = False
