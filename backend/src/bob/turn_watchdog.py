"""TurnWatchdog — wall-clock budgets around every user turn (PRD 0018 / issue 0126).

Before this module, a provider that silently hung mid-turn produced ETERNAL
SILENCE: the text path parked forever inside ``process_user_message`` (the
WS receive loop with it), and the voice say-path task simply never returned —
no event, no fallback, an FSM stuck in ``thinking``. The watchdog bounds every
user turn (text or voice) with TWO distinct budgets:

- **TTFT** (time-to-first-token, short — "the provider never *started*
  answering"). The orchestrator's streaming loop calls
  :func:`note_first_token_current` on the first provider chunk (and the voice
  loop on the first outbound audio chunk for the committed-draft path, which
  never runs the LLM); once noted, the TTFT timer is disarmed.
- **completion** (the whole-turn wall clock, longer — "the provider started
  but stalled"). Measured from :meth:`TurnWatchdog.guard` entry, so a stream
  that produces a first token and then hangs is cut at the completion budget,
  NOT at TTFT.

On expiry the guard cancels the turn body and raises :class:`TurnTimeoutError`
carrying the phase, so the call site can emit the ``turn_timeout`` event,
restore its state machine to a healthy state and deliver the short fallback
(:data:`TURN_TIMEOUT_FALLBACK_SPEECH` — verbal on the voice path, text
otherwise) instead of silence. A budget ``<= 0`` disables that phase.

How the two phases run
----------------------

:meth:`TurnWatchdog.guard` spawns the turn body as a task (so the ContextVar
:data:`current_turn_watchdog` set just before creation is visible inside it)
and a SUPERVISED timer task (issue 0124 — a bug in the watchdog itself is
logged + surfaced as a debug event rather than rotting unobserved). The timer
waits for the first-token signal under the TTFT budget, then sleeps out the
remaining completion budget; on expiry it records the phase and cancels the
body. ``guard`` translates that cancellation into :class:`TurnTimeoutError`
— while an EXTERNAL cancellation (barge-in, ``voice_stop``, socket close,
detected via :meth:`asyncio.Task.cancelling` on the awaiting task) still
propagates as :class:`asyncio.CancelledError`, killing the body with it.

Call sites (issue 0126):

- :mod:`bob.ws_router` text path — wraps ``process_user_message``; budgets
  ``TURN_TTFT_TIMEOUT_SECONDS`` / ``TURN_COMPLETION_TIMEOUT_SECONDS``.
- :mod:`bob.voice_loop` say-path — wraps the whole say-path driver (LLM +
  TTS streaming); distinct budgets ``VOICE_TURN_TTFT_TIMEOUT_SECONDS`` /
  ``VOICE_TURN_COMPLETION_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import Any, Literal

import structlog

from bob.task_supervisor import create_supervised_task

_logger = structlog.get_logger(__name__)


TimeoutPhase = Literal["ttft", "completion"]


#: The short fallback delivered instead of eternal silence when a turn's
#: budget expires — spoken via TTS on the voice path (``prepared_reply``),
#: sent as a regular ``assistant_msg`` on the text path.
TURN_TIMEOUT_FALLBACK_SPEECH = (
    "Désolé, je n'arrive pas à formuler de réponse pour l'instant. On réessaie ?"
)


class TurnTimeoutError(Exception):
    """A guarded turn exceeded one of its wall-clock budgets.

    ``phase`` says WHICH budget expired: ``"ttft"`` (the provider never
    started answering within the time-to-first-token window) or
    ``"completion"`` (the turn started but did not finish within the total
    budget). ``budget_seconds`` is the expired budget's configured value, so
    the ``turn_timeout`` event can name it without re-reading settings.

    Deliberately NOT a :class:`TimeoutError` subclass: the ws_router already
    maps ``TimeoutError`` / ``openai.APITimeoutError`` to the legacy
    ``LLM_TIMEOUT`` error frame, and the watchdog path must stay distinct
    (it delivers a fallback reply, not an error frame).
    """

    def __init__(self, phase: TimeoutPhase, *, budget_seconds: float) -> None:
        super().__init__(f"turn {phase} budget exhausted ({budget_seconds:g}s)")
        self.phase: TimeoutPhase = phase
        self.budget_seconds = budget_seconds


#: The watchdog guarding the turn the current task belongs to. Bound by
#: :meth:`TurnWatchdog.guard` just before it spawns the body task (the task
#: copies the context at creation), so downstream sites that never see the
#: watchdog instance — the orchestrator's streaming loop — can disarm the
#: TTFT timer via :func:`note_first_token_current`. ``None`` (the default —
#: unguarded paths, narrow tests) makes the helper a no-op.
current_turn_watchdog: ContextVar[TurnWatchdog | None] = ContextVar(
    "current_turn_watchdog", default=None
)


def note_first_token_current() -> None:
    """Disarm the TTFT timer of the watchdog bound to the current context.

    Called by the orchestrator on the FIRST streamed provider chunk (the
    "the provider started answering" signal). A no-op when no watchdog is
    bound (text sites running outside a guarded turn, unit tests) — never
    raises, mirroring :func:`bob.turn_metrics.mark_current`.
    """

    watchdog = current_turn_watchdog.get()
    if watchdog is not None:
        watchdog.note_first_token()


class TurnWatchdog:
    """Two-phase (TTFT + completion) wall-clock guard for one user turn.

    One instance guards ONE turn (the first-token latch is single-shot).
    Budgets ``<= 0`` disable the corresponding phase; with both disabled
    :meth:`guard` degrades to a plain await of the body.
    """

    def __init__(self, *, ttft_timeout_s: float, completion_timeout_s: float) -> None:
        self._ttft_s = ttft_timeout_s
        self._completion_s = completion_timeout_s
        self._first_token = asyncio.Event()
        self._expired: TurnTimeoutError | None = None

    @property
    def first_token_seen(self) -> bool:
        """True once the provider produced its first token (TTFT disarmed)."""

        return self._first_token.is_set()

    def note_first_token(self) -> None:
        """Latch "the provider started answering" — disarms the TTFT timer."""

        self._first_token.set()

    async def guard[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> T:
        """Run ``coro`` under the two budgets; raise :class:`TurnTimeoutError` on expiry.

        The body runs as a child task so the watchdog ContextVar binds into
        it; the timer runs as a SUPERVISED side task (issue 0124). Outcomes:

        - body finishes within budget → its result is returned (an exception
          it raised propagates unchanged);
        - a budget expires → the body is cancelled and the recorded
          :class:`TurnTimeoutError` is raised;
        - the CALLER is cancelled (barge-in / ``voice_stop`` / socket close)
          → the body is cancelled too and :class:`asyncio.CancelledError`
          propagates (never converted into a timeout).
        """

        token = current_turn_watchdog.set(self)
        try:
            body: asyncio.Task[T] = asyncio.create_task(coro, name=name)
        finally:
            current_turn_watchdog.reset(token)
        # Issue 0124 — the watchdog itself is supervised: a bug escaping the
        # timer coroutine is logged + surfaced on /ws/debug instead of dying
        # silently on the task (which would leave the turn unguarded).
        timer = create_supervised_task(
            self._watch(body),
            name=f"{name}.watchdog",
            session_id=session_id,
            turn_id=turn_id,
            context={
                "ttft_timeout_s": self._ttft_s,
                "completion_timeout_s": self._completion_s,
            },
        )
        try:
            return await body
        except asyncio.CancelledError:
            current = asyncio.current_task()
            externally_cancelled = current is not None and current.cancelling() > 0
            if self._expired is not None and not externally_cancelled:
                # Our own timer cut the body — translate into the typed
                # timeout so call sites can deliver the fallback.
                raise self._expired from None
            # External cancellation: the body must not outlive the caller.
            await _kill(body)
            raise
        finally:
            if not timer.done():
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await timer

    async def _watch(self, body: asyncio.Task[Any]) -> None:
        """The timer: TTFT phase, then completion phase; cancel ``body`` on expiry."""

        loop = asyncio.get_running_loop()
        started = loop.time()
        ttft_s = self._ttft_s if self._ttft_s > 0 else None
        completion_s = self._completion_s if self._completion_s > 0 else None

        if ttft_s is not None and not self._first_token.is_set():
            # The TTFT window never outlasts the completion budget: a provider
            # that never answers under ttft >= completion is a completion cut.
            phase_budget = ttft_s if completion_s is None else min(ttft_s, completion_s)
            try:
                await asyncio.wait_for(self._first_token.wait(), phase_budget)
            except TimeoutError:
                if completion_s is not None and phase_budget >= completion_s:
                    self._expired = TurnTimeoutError("completion", budget_seconds=completion_s)
                else:
                    self._expired = TurnTimeoutError("ttft", budget_seconds=ttft_s)
                _logger.warning(
                    "turn_watchdog.expired",
                    phase=self._expired.phase,
                    budget_seconds=self._expired.budget_seconds,
                )
                body.cancel()
                return

        if completion_s is None:
            return
        remaining = completion_s - (loop.time() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._expired = TurnTimeoutError("completion", budget_seconds=completion_s)
        _logger.warning(
            "turn_watchdog.expired",
            phase="completion",
            budget_seconds=completion_s,
        )
        body.cancel()


async def _kill(body: asyncio.Task[Any]) -> None:
    """Cancel ``body`` and consume its outcome (idempotent on a done task)."""

    if body.done():
        return
    body.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await body


__all__ = [
    "TURN_TIMEOUT_FALLBACK_SPEECH",
    "TimeoutPhase",
    "TurnTimeoutError",
    "TurnWatchdog",
    "current_turn_watchdog",
    "note_first_token_current",
]
