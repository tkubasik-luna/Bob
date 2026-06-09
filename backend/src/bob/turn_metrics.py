"""Per-turn critical-path metrics collector (PRD 0018 / issue 0117, Module 1).

PRD 0018 starts with "mesurer d'abord": before any latency optimization lands,
every voice turn must produce a chronometered decomposition of its critical
path so each later module's gain is a measurable delta. This module is the ONE
place that owns that bookkeeping — a :class:`TurnLatencyMetrics` collector
keyed by turn id with a deliberately minimal surface:

- :meth:`TurnLatencyMetrics.begin_turn` — register a turn (its time origin);
- :meth:`TurnLatencyMetrics.mark` — stamp a named critical-path stage
  (:data:`STAGE_NAMES`: ``endpoint``, ``stt_finalized``, ``loops_frozen``,
  ``gate_decided``, ``llm_first_token``, ``tts_first_chunk``,
  ``audio_first_byte``) on the collector's monotone clock;
- :meth:`TurnLatencyMetrics.count` — bump a per-turn counter
  (:data:`COUNTER_NAMES`: draft adopted/discarded, validation retries);
- :meth:`TurnLatencyMetrics.finish_turn` — project the per-turn summary
  (duration of each stage), feed the rolling aggregates, evict the turn;
- :meth:`TurnLatencyMetrics.aggregates` — rolling P50/P95 per stage plus the
  cumulative counter totals (draft adoption rate, retry distribution).

Everything is in-memory and bounded (no persistence): at most ``max_turns``
turns are tracked at once (the oldest is evicted when a pathological producer
opens turns it never finishes) and each stage's percentile window holds at
most ``window`` samples — a long session can never grow memory. ``mark`` /
``count`` on an unknown turn id is a SAFE NO-OP by contract: producers on the
text path (no voice turn registered) or racing a finished turn must never
raise.

How the marks reach the collector
---------------------------------

The full-duplex loop (:mod:`bob.voice_loop`) knows its turn id and calls the
collector directly (``begin_turn`` at speech start, ``mark`` on the endpoint
path, ``finish_turn`` + the ``turn_metrics`` debug event at turn end). The
downstream say-path sites (the orchestrator's first LLM token, the TTS
first-chunk / first-byte in ws_router, the validation-retry loop) do NOT know
the voice turn id — they run inside the say-path task, so the loop binds
:data:`current_metrics_turn_id` (a ``ContextVar``) for the task's duration and
those sites call :func:`mark_current` / :func:`count_current`, which resolve
the id from the context. On a text turn the ContextVar is unset and both
helpers no-op — the text path pays nothing.

A ``mark`` is FIRST-write-wins per stage: a validation retry that streams a
second LLM attempt does not move ``llm_first_token`` (the retry shows up in
the ``validation_retry`` counter instead), so the per-stage durations always
describe the first time the pipeline reached each stage.

Like :mod:`bob.event_retention_policy`, the process-wide default collector is
a module singleton installed by :mod:`bob.main` from settings
(``TURN_METRICS_MAX_TURNS`` / ``TURN_METRICS_WINDOW``) and reset on teardown;
:func:`get_default_collector` always returns a live instance so producers
never need a None-guard.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

#: Canonical critical-path stages (PRD 0018 / Module 1). ``mark`` accepts any
#: stage name for the per-turn summary, but only the canonical ones feed the
#: rolling P50/P95 windows — the aggregate key set stays bounded by design.
STAGE_NAMES: tuple[str, ...] = (
    "endpoint",
    "stt_finalized",
    "loops_frozen",
    "gate_decided",
    "llm_first_token",
    "tts_first_chunk",
    "audio_first_byte",
)

#: Counters every summary carries (0 when never bumped — stable schema, like
#: the feature-gated keys of :meth:`bob.latency.TurnLatency.derived`).
COUNTER_NAMES: tuple[str, ...] = ("draft_adopted", "draft_discarded", "validation_retry")

#: The voice turn id the in-flight say-path task is attributed to. Bound by
#: :meth:`bob.voice_loop.FullDuplexLoop._run_say_path` for the task's duration
#: so the orchestrator / ws_router instrumentation sites (which never see the
#: voice turn id) can stamp into the right turn via :func:`mark_current`.
#: ``None`` (the default — text turns, proactive TTS, backchannels) makes the
#: ``*_current`` helpers a no-op.
current_metrics_turn_id: ContextVar[str | None] = ContextVar(
    "current_metrics_turn_id", default=None
)


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile over an ascending-sorted, non-empty list."""

    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


@dataclass
class _TurnEntry:
    """Bookkeeping for one in-flight turn (private to the collector)."""

    began_at: float
    #: ``{stage: monotone_seconds}`` — first-write-wins per stage.
    marks: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)


class TurnLatencyMetrics:
    """In-memory, bounded, per-turn latency + counter collector (issue 0117).

    ``clock`` is injectable (monotone seconds; defaults to ``time.monotonic``)
    so tests drive deterministic durations. ``max_turns`` bounds the number of
    simultaneously-tracked turns (oldest evicted); ``window`` bounds each
    stage's rolling percentile sample count. All projections report durations
    in **milliseconds** (matching :mod:`bob.latency`'s derived metrics).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_turns: int = 64,
        window: int = 256,
    ) -> None:
        self._clock = clock
        self._max_turns = max(1, max_turns)
        self._window = max(1, window)
        self._turns: OrderedDict[str, _TurnEntry] = OrderedDict()
        #: Rolling per-stage duration windows (ms) — canonical stages only, so
        #: the key set (and therefore memory) is bounded by :data:`STAGE_NAMES`.
        self._stage_windows: dict[str, deque[float]] = {}
        self._counter_totals: dict[str, int] = dict.fromkeys(COUNTER_NAMES, 0)
        self._turns_measured = 0

    # -- recording -------------------------------------------------------------

    def begin_turn(self, turn_id: str) -> None:
        """Register ``turn_id`` with the current clock as its time origin.

        Re-beginning an id resets it to a fresh entry — the barge-in path
        re-uses the interrupted turn's id for the resumed utterance (the 0101
        FSM contract), so the resumed turn measures from its own origin rather
        than inheriting the cut turn's marks. Beyond ``max_turns`` the oldest
        tracked turn is silently evicted (bounded retention).
        """

        self._turns.pop(turn_id, None)
        self._turns[turn_id] = _TurnEntry(began_at=self._clock())
        while len(self._turns) > self._max_turns:
            self._turns.popitem(last=False)

    def mark(self, turn_id: str, stage: str) -> None:
        """Stamp ``stage`` for ``turn_id`` (first-write-wins; unknown id = no-op)."""

        entry = self._turns.get(turn_id)
        if entry is None or stage in entry.marks:
            return
        entry.marks[stage] = self._clock()

    def count(self, turn_id: str, counter: str, n: int = 1) -> None:
        """Bump ``counter`` for ``turn_id`` by ``n`` (unknown id = no-op)."""

        entry = self._turns.get(turn_id)
        if entry is None:
            return
        entry.counters[counter] = entry.counters.get(counter, 0) + n

    # -- projections -----------------------------------------------------------

    def finish_turn(self, turn_id: str) -> dict[str, object] | None:
        """Close ``turn_id``: return its summary and feed the rolling aggregates.

        The summary body (``None`` on an unknown / already-finished id):

        - ``stages_ms`` — duration of each stamped stage = delta from the
          PREVIOUS stamped mark in chronological order (the first stage
          measures from ``begin_turn``, i.e. ``endpoint`` carries the user's
          speaking time, every later stage the pipeline step it crossed);
        - ``marks`` — the raw monotone stamps (same convention as the Annexe F
          ``turn_latency`` event, for correlation);
        - ``total_ms`` — first mark → last mark (0.0 when fewer than two);
        - ``counters`` — always carries :data:`COUNTER_NAMES` (0 default) plus
          any extra counter a producer bumped.

        Canonical stage durations are pushed into the bounded percentile
        windows; counters accumulate into the process totals — both feed
        :meth:`aggregates`.
        """

        entry = self._turns.pop(turn_id, None)
        if entry is None:
            return None

        ordered = sorted(entry.marks.items(), key=lambda kv: kv[1])
        stages_ms: dict[str, float] = {}
        previous = entry.began_at
        for stage, stamped in ordered:
            stages_ms[stage] = round((stamped - previous) * 1000.0, 3)
            previous = stamped
        total_ms = 0.0
        if len(ordered) >= 2:
            total_ms = round((ordered[-1][1] - ordered[0][1]) * 1000.0, 3)

        counters: dict[str, int] = dict.fromkeys(COUNTER_NAMES, 0)
        counters.update(entry.counters)

        for stage, duration_ms in stages_ms.items():
            if stage not in STAGE_NAMES:
                continue
            window = self._stage_windows.get(stage)
            if window is None:
                window = deque(maxlen=self._window)
                self._stage_windows[stage] = window
            window.append(duration_ms)
        for counter, value in counters.items():
            self._counter_totals[counter] = self._counter_totals.get(counter, 0) + value
        self._turns_measured += 1

        return {
            "turn_id": turn_id,
            "stages_ms": stages_ms,
            "marks": dict(ordered),
            "total_ms": total_ms,
            "counters": counters,
        }

    def aggregates(self) -> dict[str, object]:
        """Rolling P50/P95 per canonical stage + cumulative counter health.

        ``stages`` carries one ``{count, p50_ms, p95_ms}`` row per stage that
        has at least one sample in its window. ``counters_total`` is the
        process-lifetime accumulation; ``draft_adoption_rate`` derives from it
        (``None`` until the commit gate has judged at least one draft, so the
        rate is never a bogus 0/0).
        """

        stages: dict[str, dict[str, float | int]] = {}
        for stage in STAGE_NAMES:
            window = self._stage_windows.get(stage)
            if not window:
                continue
            ordered = sorted(window)
            stages[stage] = {
                "count": len(ordered),
                "p50_ms": round(_percentile(ordered, 0.50), 3),
                "p95_ms": round(_percentile(ordered, 0.95), 3),
            }
        adopted = self._counter_totals.get("draft_adopted", 0)
        discarded = self._counter_totals.get("draft_discarded", 0)
        judged = adopted + discarded
        rate = round(adopted / judged, 3) if judged else None
        return {
            "turns_measured": self._turns_measured,
            "stages": stages,
            "counters_total": dict(self._counter_totals),
            "draft_adoption_rate": rate,
        }


# -- process-wide default collector (installed by bob.main) ---------------------

_default_collector: TurnLatencyMetrics = TurnLatencyMetrics()


def set_default_collector(collector: TurnLatencyMetrics | None) -> None:
    """Install the process-wide collector (``None`` resets to a fresh default)."""

    global _default_collector
    _default_collector = collector if collector is not None else TurnLatencyMetrics()


def get_default_collector() -> TurnLatencyMetrics:
    """Return the process-wide collector — always a live instance."""

    return _default_collector


def mark_current(stage: str) -> None:
    """Stamp ``stage`` on the turn bound to :data:`current_metrics_turn_id`.

    The instrumentation entry point for say-path sites that do not know the
    voice turn id (orchestrator first token, ws_router TTS chunks). A no-op
    outside a metered voice turn (text path / proactive TTS) and on an
    unknown/finished turn id — never raises.
    """

    turn_id = current_metrics_turn_id.get()
    if turn_id is not None:
        _default_collector.mark(turn_id, stage)


def count_current(counter: str, n: int = 1) -> None:
    """Bump ``counter`` on the turn bound to :data:`current_metrics_turn_id`.

    Same no-op contract as :func:`mark_current` (used by the validation-retry
    loop, which also runs on text turns where no voice turn is bound).
    """

    turn_id = current_metrics_turn_id.get()
    if turn_id is not None:
        _default_collector.count(turn_id, counter, n)


__all__ = [
    "COUNTER_NAMES",
    "STAGE_NAMES",
    "TurnLatencyMetrics",
    "count_current",
    "current_metrics_turn_id",
    "get_default_collector",
    "mark_current",
    "set_default_collector",
]
