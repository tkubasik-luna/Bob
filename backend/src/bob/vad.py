"""Voice-activity detection over inbound mic PCM frames (issue 0100).

The full-duplex loop (PRD 0016, Annexe B) needs to know *when the user is
talking* to drive the :class:`bob.turn_fsm.TurnFsm`. The first decision point
is purely acoustic: is this 16 kHz mono s16le frame speech or silence?

This module is a deliberately tiny **energy-threshold VAD** — the issue 0100
spec explicitly allows it ("energy-threshold VAD is fine; thresholds
configurable"). A learned / spectral VAD is a future improvement; the FSM only
needs a robust speech/pause edge today.

Design
------

:class:`EnergyVad` is pure + synchronous: feed it one decoded PCM payload at a
time (the tag-stripped bytes :func:`bob.stt_engine.decode_pcm_frame` returns)
and it emits at most one :class:`VadEvent` per frame — a *transition*:

- ``vad_speech_start`` — the stream crossed from silence into speech. Fires on
  the first frame whose RMS is at/above ``speech_rms`` while the detector was
  idle.
- ``vad_pause`` — the stream crossed from speech into a pause. Fires once a run
  of consecutive sub-threshold frames reaches ``pause_frames`` while the
  detector was active.

Hysteresis (two ideas, both configurable):

1. A run of ``pause_frames`` quiet frames is required before declaring a pause,
   so a single quiet frame inside an utterance (a stop consonant, a breath)
   does NOT spuriously emit ``vad_pause``. ``pause_frames`` is a *count of
   frames*, kept frame-rate-relative so the caller can reason in milliseconds
   via :meth:`frames_for_ms`.
2. Speech is declared on the first loud frame (fast attack) — we want Bob to
   register "the user started" with minimal latency; the confirmation cost is
   spent on the *release* (pause) side, which is the one that gates the
   endpoint.

The detector is edge-triggered: it returns ``None`` for frames that do not
change the speaking state, so the caller can route each event straight to the
FSM without de-duping. :class:`bob.endpointer.Endpointer` consumes the
``vad_pause`` / speech edges to decide the silence-floor ``endpoint``.

RMS, not peak: a single sample spike (click) should not read as speech, and a
sustained low hum should not read as silence — root-mean-square over the frame
is the cheap robust middle. Empty frames read as silence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from bob.stt_engine import pcm16_sample_count

#: s16le full-scale magnitude — RMS is reported normalised to ``[0, 1]`` so the
#: configured threshold is independent of the 16-bit sample width.
_INT16_FULL_SCALE = 32768.0


class VadEvent(StrEnum):
    """A speech/silence *transition* emitted by :class:`EnergyVad`.

    Values are the wire event names (Annexe A.2 / Annexe B) so the WS layer can
    forward ``event.value`` directly without a second mapping table.
    """

    SPEECH_START = "vad_speech_start"
    PAUSE = "vad_pause"


def rms_normalised(pcm: bytes) -> float:
    """Return the RMS amplitude of an s16le PCM payload, normalised to ``[0, 1]``.

    ``pcm`` is the tag-stripped payload (whole number of 16-bit little-endian
    samples) — exactly what :func:`bob.stt_engine.decode_pcm_frame` yields. An
    empty payload returns ``0.0`` (silence). Pure stdlib (no numpy) so the
    detector stays importable anywhere and fast for the ~480-sample frames the
    webview ships.
    """

    n = pcm16_sample_count(pcm)
    if n == 0:
        return 0.0
    total_sq = 0.0
    # ``int.from_bytes`` per sample would be slow; slice the buffer as a memory
    # view and decode signed 16-bit little-endian in a tight loop.
    mv = memoryview(pcm)
    for i in range(0, n * 2, 2):
        sample = int.from_bytes(mv[i : i + 2], "little", signed=True)
        total_sq += float(sample) * float(sample)
    return math.sqrt(total_sq / n) / _INT16_FULL_SCALE


@dataclass
class EnergyVad:
    """Edge-triggered energy VAD (issue 0100).

    Construct with the (normalised) RMS threshold + the pause confirmation
    length, then call :meth:`feed_frame` for each decoded PCM payload. Each
    call returns the *transition* the frame caused, or ``None`` for no change:

    - silence → speech yields :attr:`VadEvent.SPEECH_START`;
    - speech → (``pause_frames`` consecutive quiet frames) yields
      :attr:`VadEvent.PAUSE`.

    The instance is stateful (it tracks the speaking flag + the trailing quiet
    run) and scoped to ONE voice turn; build a fresh one per turn (or call
    :meth:`reset`).

    Thresholds are constructor args (the WS layer wires them from
    :class:`bob.config.Settings`) so nothing is hard-coded on the hot path.
    """

    #: Frames at/above this normalised RMS are speech (fast attack).
    speech_rms: float = 0.02
    #: Consecutive quiet frames required to declare a pause (release hysteresis).
    pause_frames: int = 10
    #: Mic frame duration in ms — only used by :meth:`frames_for_ms` so callers
    #: can express ``pause_frames`` / endpoint windows in milliseconds.
    frame_ms: int = 30

    _speaking: bool = False
    _quiet_run: int = 0

    def __post_init__(self) -> None:
        # A non-positive pause length would emit a pause on the first quiet
        # frame, defeating the hysteresis — floor at 1.
        if self.pause_frames < 1:
            self.pause_frames = 1

    @property
    def speaking(self) -> bool:
        """Whether the detector currently considers the user to be speaking."""

        return self._speaking

    def frames_for_ms(self, ms: int) -> int:
        """Convert a duration in ms to a frame count at the configured frame size.

        Rounds to the nearest whole frame, floored at 1, so a caller can derive
        ``pause_frames`` (or an :class:`bob.endpointer.Endpointer` window) from a
        millisecond tunable without knowing the frame size.
        """

        if self.frame_ms <= 0:
            return max(1, ms)
        return max(1, round(ms / self.frame_ms))

    def feed_frame(self, pcm: bytes) -> VadEvent | None:
        """Feed one decoded PCM payload; return the transition it caused (or None).

        Loud frame while idle → :attr:`VadEvent.SPEECH_START`. Quiet frames
        while speaking accumulate; when the run reaches ``pause_frames`` →
        :attr:`VadEvent.PAUSE` (emitted once, then suppressed until speech
        resumes). All other frames return ``None``. Computes the speech/silence
        decision from the frame RMS; :meth:`observe` is the variant for a caller
        that already has the decision (avoids a second RMS pass).
        """

        return self.observe(is_speech=rms_normalised(pcm) >= self.speech_rms)

    def observe(self, *, is_speech: bool) -> VadEvent | None:
        """Advance the VAD with a precomputed per-frame speech/silence decision.

        Same edge semantics as :meth:`feed_frame` but takes the boolean
        directly, so the full-duplex loop can compute the RMS once and feed the
        same decision to both the VAD and the :class:`bob.endpointer.Endpointer`.
        """

        if is_speech:
            self._quiet_run = 0
            if not self._speaking:
                self._speaking = True
                return VadEvent.SPEECH_START
            return None

        # Quiet frame.
        if not self._speaking:
            return None
        self._quiet_run += 1
        if self._quiet_run >= self.pause_frames:
            self._speaking = False
            self._quiet_run = 0
            return VadEvent.PAUSE
        return None

    def reset(self) -> None:
        """Reset to the idle (not-speaking) state for a fresh turn."""

        self._speaking = False
        self._quiet_run = 0
