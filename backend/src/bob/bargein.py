"""Barge-in confirmation controller (PRD 0016 / issue 0101, Annexe B + F).

While Bob holds the floor (``bob_speaking``), the user may start talking to
*interrupt* him. The naive rule — "any frame of user speech cuts Bob" — is too
trigger-happy: a single noise spike, a cough, or a short backchannel ("mm-hm",
"oui") would chop Bob mid-sentence. So a real barge-in requires a **confirmation
window**: a *continuous* run of user speech long enough (default ~200-300 ms,
configurable) before we declare the interrupt and tear Bob's reply down.

This module is the pure, synchronous decision core for that rule —
deliberately isolated from asyncio / the FSM / TTS exactly like
:class:`bob.vad.EnergyVad`, so it is exhaustively unit-testable on a *simulated
timeline*. The full-duplex loop (:mod:`bob.voice_loop`) owns the effects
(cancel the LLM stream, cancel TTS, commit the spoken partial, restart the
Thinker); this object only answers the one question: *given the frames seen so
far while Bob speaks, has a barge-in been confirmed, and if so when did the
user's continuous speech begin?*

Design
------

:class:`BargeInController` is fed one frame at a time via :meth:`observe`, with

- ``is_speech`` — the same per-frame energy-VAD decision the loop already
  computes (one RMS pass, reused — see :meth:`bob.voice_loop.FullDuplexLoop`);
- ``now`` — a monotonic timestamp in seconds (the loop passes ``time.monotonic``
  results; tests pass a synthetic timeline).

It tracks the timestamp at which the *current* continuous speech run started
(``_run_started``). A non-speech frame resets that run to ``None`` — this is the
hysteresis that filters short backchannels: a 100 ms "oui" never accumulates the
full window because the trailing silence clears the run before it crosses the
threshold. Once a run's duration reaches :attr:`confirm_ms`, :meth:`observe`
returns a :class:`BargeInConfirmation` carrying:

- ``detected_ts`` — when the *continuous* speech that triggered the cut began
  (the run start). This is Annexe F's ``t_bargein_detected``: latency is measured
  from the moment the user actually started talking, NOT from the late frame that
  happened to cross the window.
- ``confirmed_ts`` — the timestamp of the frame that crossed the window (the
  earliest instant the loop is *allowed* to cut; the loop's own ``t_cut`` is
  stamped when it actually cancels, ≥ this).

After it fires once it latches (``_fired``) and returns ``None`` for every
subsequent frame, so the loop drives the single ``bargein_confirmed`` edge
exactly once per controller. Build a fresh controller per ``bob_speaking``
window (or call :meth:`reset`) — the loop does this on each entry into
``bob_speaking``.

Why monotonic timestamps rather than a frame count?
----------------------------------------------------

Frames are nominally ~30 ms but the wire can jitter / drop; gating on a real
elapsed-time window keeps the 200-300 ms target honest regardless of frame
cadence, and makes the simulated-timeline tests express the contract directly in
milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Default confirmation window in ms — the low end of Annexe B's 200-300 ms band.
#: The loop wires this from :attr:`bob.config.Settings.BARGEIN_CONFIRM_MS`.
DEFAULT_CONFIRM_MS = 200


@dataclass(frozen=True)
class BargeInConfirmation:
    """A confirmed barge-in (the result of :meth:`BargeInController.observe`).

    ``detected_ts`` is the monotonic timestamp at which the user's *continuous*
    speech began (Annexe F ``t_bargein_detected``); ``confirmed_ts`` is the
    timestamp of the frame that crossed the confirmation window (the earliest
    legal cut instant). Both are in seconds on the same monotonic clock the
    caller fed to :meth:`observe`.
    """

    detected_ts: float
    confirmed_ts: float


@dataclass
class BargeInController:
    """Confirmation-window barge-in detector (pure, simulated-timeline testable).

    Construct with the confirmation window (``confirm_ms``), then feed it the
    per-frame ``(is_speech, now)`` decisions observed *while Bob is speaking*.
    :meth:`observe` returns a :class:`BargeInConfirmation` on the frame that
    confirms the interrupt, else ``None``. It latches after firing (one
    confirmation per window) and resets the speech run on any non-speech frame
    (so sub-window backchannels never accumulate the full window).
    """

    #: Continuous speech (ms) required before a barge-in is confirmed.
    confirm_ms: int = DEFAULT_CONFIRM_MS

    #: Monotonic timestamp (s) at which the current continuous speech run began,
    #: or ``None`` when the last frame was silence (no run in progress).
    _run_started: float | None = None
    #: Latch — once a barge-in fired we ignore later frames until :meth:`reset`.
    _fired: bool = False

    def __post_init__(self) -> None:
        # A non-positive window would confirm on the first speech frame, undoing
        # the backchannel filter — floor at 1 ms so a run must span >= one frame.
        if self.confirm_ms < 1:
            self.confirm_ms = 1

    @property
    def armed(self) -> bool:
        """True while the controller can still fire (not yet latched)."""

        return not self._fired

    @property
    def run_started(self) -> float | None:
        """Start timestamp of the in-progress speech run (``None`` when silent)."""

        return self._run_started

    def observe(self, *, is_speech: bool, now: float) -> BargeInConfirmation | None:
        """Advance the controller with one frame; return a confirmation or ``None``.

        Speech frame: start the run (stamp ``_run_started``) if none is open,
        then check whether the run has lasted ``confirm_ms`` — if so, latch and
        return the :class:`BargeInConfirmation` (``detected_ts`` = run start,
        ``confirmed_ts`` = ``now``).

        Silence frame: clear the in-progress run (the backchannel filter — a
        short burst followed by silence never reaches the window).

        Once latched (:attr:`armed` is False) every call returns ``None``.
        """

        if self._fired:
            return None

        if not is_speech:
            # End of a (sub-window) speech run — reset so the next run starts
            # fresh. This is what makes a brief "mm-hm" harmless.
            self._run_started = None
            return None

        if self._run_started is None:
            self._run_started = now

        elapsed_ms = (now - self._run_started) * 1000.0
        if elapsed_ms >= self.confirm_ms:
            self._fired = True
            return BargeInConfirmation(detected_ts=self._run_started, confirmed_ts=now)
        return None

    def reset(self) -> None:
        """Reset to the un-fired, no-run state for a fresh ``bob_speaking`` window."""

        self._run_started = None
        self._fired = False
