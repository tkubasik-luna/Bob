"""Per-turn latency accumulator for the full-duplex loop (issue 0110, Annexe F).

PRD 0016 / Annexe F is *normative*: every real-time turn stamps a set of
**marks** on a monotone server clock and the loop emits one ``turn_latency``
event ``{turn_id, marks, derived}`` at turn end (the completed AND the barge-in
paths). The marks are produced by several upstream slices — the 0100/0101 loop
already stamps ``t_first_mic_frame`` / ``t_endpoint`` / ``t_first_audio_chunk``
and the barge-in ``t_bargein_detected`` / ``t_cut``; the Draft slice (0104) will
add ``t_draft_ready`` / ``t_commit_decision``; backchannels (0105) feed
``backchannel_ms``. Rather than scatter the mark bookkeeping + the derived-metric
arithmetic across :mod:`bob.voice_loop`, this module is the ONE place that owns:

- the canonical mark set (one slot per Annexe F mark, ``None`` until stamped);
- the ``marks`` payload projection (only the stamped marks, plain floats);
- the ``derived`` computation (pure arithmetic over the marks).

The loop holds a :class:`TurnLatency` per turn, slices stamp into it, and the
loop emits :meth:`TurnLatency.as_event_body` once at finalize. Keeping the
arithmetic here makes every derived metric independently unit-testable without a
running loop, and keeps the missing-mark handling (a derived is simply absent
when its inputs are unset) in a single, audited spot.

Marks are *monotone seconds* (``time.monotonic``); the derived deltas are
reported in **milliseconds** to match Annexe F's targets (e.g.
``endpoint_to_first_audio_ms < 800``). ``backchannel_ms`` stays ``None`` until
0105 and ``draft_hit`` stays ``False`` until 0104 — the fields exist now (a
stable schema for the persisted ``latency_json`` + the harness) but carry their
"feature not wired" defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A derived-metrics payload value: a numeric delta (ms), a bool (``draft_hit``),
#: or ``None`` (a metric whose feature is not wired / inputs are missing).
DerivedValue = float | bool | None


def _delta_ms(start: float | None, end: float | None) -> float | None:
    """Return ``(end - start)`` in milliseconds, or ``None`` if either is unset.

    The single missing-mark rule for every mark→mark derived: a metric is simply
    absent (``None``) when one of its endpoints was never stamped, never a bogus
    zero or a negative artefact of a half-built turn.
    """

    if start is None or end is None:
        return None
    return round((end - start) * 1000.0, 3)


@dataclass
class TurnLatency:
    """Annexe F latency marks + derived metrics for ONE turn.

    Every field is a monotone-second timestamp (``None`` = not yet stamped).
    Slices assign the marks they own directly (``lat.t_endpoint = now``); the
    loop calls :meth:`marks_payload` / :meth:`derived` (or the combined
    :meth:`as_event_body`) at finalize. ``draft_hit`` is the only non-timestamp
    field — a bool the Draft slice (0104) flips to ``True`` when Bob spoke a
    committed speculative draft rather than a cold generation.
    """

    #: First inbound mic frame of the session/turn (when Bob started listening).
    t_first_mic_frame: float | None = None
    #: First STT partial of this turn (the first time we had *any* hypothesis).
    t_first_partial: float | None = None
    #: Endpoint — the silence floor / ``user_turn_complete`` froze the turn.
    t_endpoint: float | None = None
    #: Draft ready (issue 0104 — speculative draft generated). Placeholder today.
    t_draft_ready: float | None = None
    #: Commit decision (issue 0104 — draft committed vs cold). Placeholder today.
    t_commit_decision: float | None = None
    #: First outbound TTS chunk left the socket (Bob got the floor / spoke).
    t_first_audio_chunk: float | None = None
    #: Last outbound TTS chunk / synthesis done (Bob finished speaking).
    t_tts_end: float | None = None
    #: Barge-in: when the user's continuous speech that confirmed the cut began.
    t_bargein_detected: float | None = None
    #: Barge-in: when the loop actually cancelled Bob's in-flight reply.
    t_cut: float | None = None

    #: Did Bob speak a committed speculative Draft (vs a cold generation)?
    #: Issue 0104 flips this; ``False`` until then.
    draft_hit: bool = False

    def marks_payload(self) -> dict[str, float]:
        """The stamped marks as a plain ``{name: monotone_seconds}`` dict.

        Only marks that were actually stamped appear — an unset mark is omitted
        rather than serialised as ``null`` so the ``turn_latency.marks`` body is
        exactly "what happened this turn" (the same shape the 0100 ``_Marks``
        produced, now centralized).
        """

        candidates: dict[str, float | None] = {
            "t_first_mic_frame": self.t_first_mic_frame,
            "t_first_partial": self.t_first_partial,
            "t_endpoint": self.t_endpoint,
            "t_draft_ready": self.t_draft_ready,
            "t_commit_decision": self.t_commit_decision,
            "t_first_audio_chunk": self.t_first_audio_chunk,
            "t_tts_end": self.t_tts_end,
            "t_bargein_detected": self.t_bargein_detected,
            "t_cut": self.t_cut,
        }
        return {name: value for name, value in candidates.items() if value is not None}

    def derived(self) -> dict[str, DerivedValue]:
        """The Annexe F derived metrics (pure arithmetic over the marks).

        - ``endpoint_to_first_audio_ms`` = ``t_first_audio_chunk - t_endpoint``
          (target <800 ms committed / <1500 ms cold) — present only when both
          marks exist.
        - ``bargein_cut_ms`` = ``t_cut - t_bargein_detected`` (target <300 ms) —
          present only on a barged-in turn.
        - ``backchannel_ms`` — ``None`` until issue 0105 wires the backchannel
          mark (the key is always present so the schema is stable).
        - ``draft_hit`` — the bool from the Draft slice (``False`` until 0104).

        A mark→mark metric whose inputs are missing is OMITTED (the
        feature-gated ``backchannel_ms`` / ``draft_hit`` keys are always present
        so consumers can rely on them).
        """

        out: dict[str, DerivedValue] = {}
        endpoint_to_audio = _delta_ms(self.t_endpoint, self.t_first_audio_chunk)
        if endpoint_to_audio is not None:
            out["endpoint_to_first_audio_ms"] = endpoint_to_audio
        bargein_cut = _delta_ms(self.t_bargein_detected, self.t_cut)
        if bargein_cut is not None:
            out["bargein_cut_ms"] = bargein_cut
        # Feature-gated derived: always present, carrying their not-wired default
        # so the persisted latency_json + the harness see a stable schema.
        out["backchannel_ms"] = None  # issue 0105
        out["draft_hit"] = self.draft_hit  # issue 0104
        return out

    def as_event_body(self, turn_id: str) -> dict[str, object]:
        """The full ``turn_latency`` event body: ``{turn_id, marks, derived}``.

        The single source for both the emitted voice event (Annexe A.2) and the
        persisted ``voice_turns.latency_json`` blob, so the wire shape and the
        stored shape can never drift.
        """

        return {
            "turn_id": turn_id,
            "marks": self.marks_payload(),
            "derived": self.derived(),
        }


__all__ = ["DerivedValue", "TurnLatency"]
