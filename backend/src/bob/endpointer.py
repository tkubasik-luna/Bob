"""End-of-turn detection — the silence floor (issue 0100).

PRD 0016 / Annexe B: the ``endpoint`` event drives the
:class:`bob.turn_fsm.TurnFsm` from ``user_speaking`` → ``thinking`` (freeze the
transcript, request a reply). The PRD's full endpointer is **hybrid** — VAD
silence floor *plus* a semantic ``user_turn_complete`` signal from the Thinker
- but issue 0100 scopes ONLY the silence floor ("silence floor only here,
~500-700 ms, configurable"); the semantic signal lands in 0103.

So this module is the deterministic half: count how long the stream has been
continuously silent since the user last spoke, and declare ``endpoint`` once
that run reaches ``silence_floor_frames``. It is intentionally separate from
:class:`bob.vad.EnergyVad`:

- the VAD's ``vad_pause`` uses a *short* hysteresis (a beat inside the
  utterance) — it tells the FSM "the user paused" (a backchannel opportunity in
  later slices);
- the Endpointer's floor is *longer* — it tells the FSM "the user is done".

Both observe the same per-frame speech/silence decision, but with different
time constants, so they are composed rather than chained. The endpointer only
arms after speech has been observed (a turn that never had speech can't end),
and it fires its ``endpoint`` exactly once per turn.

Frame-driven + pure: feed it the same ``is_speech`` decision the VAD computes
(or let it compute its own from the PCM) and it returns ``True`` on the single
frame that crosses the floor. Reset per turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from bob.vad import rms_normalised


@dataclass
class Endpointer:
    """Silence-floor end-of-turn detector (issue 0100).

    Construct with the floor expressed as a frame count
    (:attr:`silence_floor_frames`) — the WS layer derives it from a
    millisecond setting via :meth:`bob.vad.EnergyVad.frames_for_ms` so the
    tunable stays human-readable. Then drive it one of two ways per frame:

    - :meth:`observe` with a precomputed ``is_speech`` bool (when the caller
      already ran the VAD on the frame — the production path, no double RMS), or
    - :meth:`feed_frame` with the raw PCM payload (it computes ``is_speech``
      itself from :attr:`speech_rms` — handy for isolation tests).

    Both return ``True`` on the frame that crosses the floor (the ``endpoint``),
    ``False`` otherwise. The detector arms only after the first speech frame and
    fires at most once until :meth:`reset`.
    """

    #: Consecutive silent frames (after speech) that constitute a finished turn.
    silence_floor_frames: int = 20
    #: RMS threshold used only by :meth:`feed_frame` (the self-deciding path).
    speech_rms: float = 0.02

    _armed: bool = False
    _fired: bool = False
    _silence_run: int = 0

    def __post_init__(self) -> None:
        if self.silence_floor_frames < 1:
            self.silence_floor_frames = 1

    @property
    def armed(self) -> bool:
        """True once speech has been seen (the floor can now fire)."""

        return self._armed

    def observe(self, *, is_speech: bool) -> bool:
        """Advance the detector with a precomputed speech/silence decision.

        Returns ``True`` exactly on the frame whose trailing silence run first
        reaches :attr:`silence_floor_frames` (the ``endpoint``). A speech frame
        arms the detector and clears the silence run; once fired the detector is
        latched until :meth:`reset`.
        """

        if self._fired:
            return False
        if is_speech:
            self._armed = True
            self._silence_run = 0
            return False
        if not self._armed:
            # Pre-speech silence never ends a turn.
            return False
        self._silence_run += 1
        if self._silence_run >= self.silence_floor_frames:
            self._fired = True
            return True
        return False

    def feed_frame(self, pcm: bytes) -> bool:
        """Compute ``is_speech`` from raw PCM, then :meth:`observe` it."""

        return self.observe(is_speech=rms_normalised(pcm) >= self.speech_rms)

    def reset(self) -> None:
        """Reset for a fresh turn (disarm + clear the silence run + un-fire)."""

        self._armed = False
        self._fired = False
        self._silence_run = 0
