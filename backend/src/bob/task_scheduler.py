"""Cap + queue of concurrently running sub-tasks (slice #0020).

The orchestrator (slice #0018) used to drive its sub-agents directly: each
``spawn_subtask`` tool call would create the task in :class:`TaskStore`,
transition it to ``running`` and schedule a :class:`SubAgentRunner`
immediately. That works fine for a single spawn but degrades the moment the
user issues a burst — every task starts, the LLM endpoint melts, and there is
no visible ordering for the user.

:class:`TaskScheduler` centralises that decision. The orchestrator hands a
freshly-created (``pending``) task off to :meth:`enqueue`. The scheduler:

- promotes the task to ``running`` and launches its runner if a slot is free
  (under :data:`bob.config.Settings.MAX_RUNNING_TASKS`); or
- leaves it in ``pending`` and waits for a slot to free up via
  :meth:`on_task_terminated`, which the asyncio done-callback on every
  runner task fires once the runner returns.

State invariants enforced here:

- ``len(self._running) <= cap`` at all times outside the lock-protected
  promotion section.
- Every ``pending → running`` promotion emits ``task_updated`` so the
  frontend sidebar reacts. ``task_created`` is *not* emitted here — that is
  the orchestrator's responsibility on first creation.
- ``recover_after_restart`` is idempotent: any task left in ``running`` by a
  previous process (i.e. the process crashed mid-run) is coerced back to
  ``pending`` by raw SQL before being re-enqueued through the normal path.

Threading model: a single :class:`asyncio.Lock` serialises promotion and
counter updates so two simultaneous ``on_task_terminated`` callbacks (or an
``on_task_terminated`` racing with an ``enqueue``) cannot both grab the same
pending row.

Structured concurrency (PRD 0006 / issue 0045)
----------------------------------------------

When the scheduler is started via :meth:`start`, every runner coroutine is
spawned inside ONE shared :class:`asyncio.TaskGroup`. The group is hosted
by a long-lived background coroutine that waits on a stop-event; on
:meth:`stop` we set the stop-event, the host coroutine cancels every
in-flight child via the group, and the ``async with`` exits cleanly. This
guarantees that an orchestrator crash propagating up to the FastAPI
lifespan cannot leak background sub-agent coroutines (PRD 0006 user story
#24).

Schedulers that do not call :meth:`start` (legacy tests, scripts that
exercise the scheduler in isolation) fall back to bare
``asyncio.create_task`` for each runner. The two paths are otherwise
behaviourally identical from the perspective of ``enqueue`` /
``on_task_terminated`` / ``cancel``.

Cancellation contract (PRD 0006 / issue 0045)
---------------------------------------------

:meth:`cancel` cooperates with the new :class:`SubAgentRunner` when one
is registered for the target task:

1. Mark the task as cancelling.
2. If a cooperative-cancel hook was registered for the task (via the
   ``coop_cancel_factory`` ctor arg, populated by the boot path with
   :meth:`SubAgentRunner.request_cancel`), invoke it. The runner will
   reach its next checkpoint (iteration boundary or pre-tool-call) and
   emit a forced ``done(cancelled, user_cancelled)`` cleanly.
3. Wait up to :data:`SubAgentPolicy.cancel_grace_seconds` (default
   ``2 s``) for the runner asyncio task to terminate of its own accord.
4. If the grace elapses without termination, escalate to
   :meth:`asyncio.Task.cancel`. The runner converts the resulting
   :class:`asyncio.CancelledError` into ``done(cancelled, hard_killed)``
   so the task row still ends with a structured terminal action.

When no cooperative hook is registered (legacy paths, tests using bare
runner factories) step 2 is skipped: we hard-cancel immediately. This
preserves the slice #0023 behaviour exactly for callers that have not
opted into the new runner.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any

import structlog

from bob import ws_events
from bob.config import Settings
from bob.debug_log import emit_debug
from bob.task_store import TaskStore, TaskStoreError

_logger = structlog.get_logger(__name__)


RunnerFactory = Callable[[str], Coroutine[Any, Any, None]]
"""Factory returning the coroutine the scheduler wraps in :func:`asyncio.create_task`.

The scheduler attaches a done-callback to that task so ``on_task_terminated``
fires as soon as the runner returns (success, failure or cancellation). The
runner does *not* call back into the scheduler — it just transitions the
task to its terminal state as before.
"""


CooperativeCancelFactory = Callable[[str], Callable[[], None] | None]
"""Factory looking up the cooperative-cancel callback for ``task_id``.

Returns ``None`` when no cooperative cancel is registered for the task —
the scheduler then falls back to immediate hard cancel. Populated by the
boot path with :meth:`SubAgentRunner.request_cancel` so the cooperative
path is the default in production while legacy tests keep the slice #0023
behaviour.
"""


#: Default cooperative-cancel grace window. Mirrors
#: ``SubAgentPolicy.cancel_grace_seconds`` so the two layers can be tuned
#: together. Kept here as a module-level constant so legacy tests (which
#: don't construct a :class:`SubAgentPolicy`) can reference it.
DEFAULT_COOP_CANCEL_GRACE_SECONDS = 2.0


class TaskScheduler:
    """Cap + queue for concurrent sub-task runners."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        cap: int,
        runner_factory: RunnerFactory,
        coop_cancel_factory: CooperativeCancelFactory | None = None,
        cancel_grace_seconds: float = DEFAULT_COOP_CANCEL_GRACE_SECONDS,
    ) -> None:
        if cap < 1:
            raise ValueError(f"TaskScheduler cap must be >= 1, got {cap}")
        self._task_store = task_store
        self._cap = cap
        self._runner_factory = runner_factory
        self._coop_cancel_factory = coop_cancel_factory
        self._cancel_grace_seconds = cancel_grace_seconds
        # Running set is the in-memory mirror of "tasks currently driven by an
        # asyncio runner task we own". Counter is ``len(self._running)``.
        self._running: set[str] = set()
        self._lock = asyncio.Lock()
        # Strong refs to "promote-after-termination" tasks. Without these
        # asyncio may garbage-collect a still-running coroutine — see PEP 715
        # discussion and ruff's RUF006. Cleared in the task's own callback.
        self._followups: set[asyncio.Task[None]] = set()
        # Slice #0023 — per-task asyncio handle so :meth:`cancel` can call
        # ``task.cancel()`` on the runner directly (real asyncio cancellation,
        # not a polled flag). Populated by :meth:`_activate`, cleared in the
        # done-callback.
        self._runners: dict[str, asyncio.Task[None]] = {}
        # Slice #0023 — task ids currently being cancelled. The runner's
        # done-callback runs after :meth:`cancel` already transitioned the
        # task to ``failed`` and persisted the reason; the callback must
        # only free the slot + promote a pending row, NOT touch state again.
        self._cancelling: set[str] = set()
        # Issue 0045 — single shared ``asyncio.TaskGroup`` hosting every
        # runner spawned while the scheduler is started. ``_task_group`` is
        # populated by :meth:`start` and torn down by :meth:`stop`; when
        # ``None`` the legacy ``asyncio.create_task`` path is used.
        self._task_group: asyncio.TaskGroup | None = None
        self._host_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def cap(self) -> int:
        return self._cap

    def running_task_ids(self) -> set[str]:
        """Snapshot of the in-memory running set — for tests + observability."""

        return set(self._running)

    async def start(self) -> None:
        """Spin up the shared :class:`asyncio.TaskGroup` host (PRD 0006 / 0045).

        Runners spawned after this call land inside the group; an
        orchestrator crash propagating up the lifespan cancels every
        in-flight runner deterministically when the host coroutine
        eventually exits the ``async with``.

        Idempotent — calling :meth:`start` twice is a no-op.
        """

        if self._task_group is not None:
            return
        self._stop_event = asyncio.Event()
        ready: asyncio.Event = asyncio.Event()

        async def _host() -> None:
            assert self._stop_event is not None
            try:
                async with asyncio.TaskGroup() as tg:
                    self._task_group = tg
                    ready.set()
                    # Block until ``stop`` is called. ``TaskGroup``'s
                    # ``__aexit__`` then waits for every spawned child to
                    # finish before returning, which is the structured-
                    # concurrency guarantee we need.
                    await self._stop_event.wait()
            finally:
                self._task_group = None

        # Run the host as a top-level task so it survives the caller
        # returning. We don't strong-ref it here; ``_host_task`` is the
        # caller's handle for :meth:`stop`.
        self._host_task = asyncio.create_task(_host())
        # Wait for the TaskGroup to be live before returning — otherwise
        # the first enqueue could lose the group reference race.
        await ready.wait()

    async def stop(self) -> None:
        """Tear down the shared :class:`asyncio.TaskGroup` host.

        Cancels every in-flight runner asyncio task (cooperative path
        not attempted at shutdown — the orchestrator is going down so a
        forced terminal ``done`` is acceptable) then awaits the host
        coroutine.

        Idempotent.
        """

        if self._host_task is None:
            return
        # Cancel every in-flight runner so the TaskGroup can drain
        # cleanly. Without this the host coroutine would block in
        # ``__aexit__`` until every runner returned naturally.
        for runner_task in list(self._runners.values()):
            if not runner_task.done():
                runner_task.cancel()
        if self._stop_event is not None:
            self._stop_event.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._host_task
        self._host_task = None
        self._stop_event = None

    async def enqueue(self, task_id: str) -> None:
        """Take ownership of a ``pending`` task, promote it if a slot is free.

        Called by the orchestrator right after ``create_task`` (the task is
        already in ``pending`` state and ``task_created`` has been emitted).
        If a slot is free, this method transitions the task to ``running``,
        emits ``task_updated`` and schedules the runner. Otherwise it leaves
        the task in ``pending`` — a subsequent :meth:`on_task_terminated`
        will pick it up.
        """

        async with self._lock:
            if len(self._running) >= self._cap:
                _logger.info(
                    "task_scheduler.queued",
                    task_id=task_id,
                    running=len(self._running),
                    cap=self._cap,
                )
                return
            self._running.add(task_id)
        # State transition + WS emit + runner scheduling happen outside the
        # lock — they touch the SQLite store (its own threading.Lock) and the
        # event loop; holding the asyncio.Lock through them would serialize
        # promotions unnecessarily and risk lock-order inversion.
        await self._activate(task_id)

    async def on_task_terminated(self, task_id: str) -> None:
        """Decrement the counter and promote the next pending task, if any.

        Fired by the runner asyncio task's done-callback. The runner itself
        has already transitioned the task to ``done`` / ``failed`` /
        ``waiting_input`` (or it crashed). We only manage scheduling state
        here. ``waiting_input`` frees the slot — :meth:`resume` re-acquires
        one when the orchestrator forwards a user answer back.

        Slice #0023: when a task is being cancelled by :meth:`cancel`,
        ``task_id`` is in :attr:`_cancelling`. We still free the slot + promote
        a pending row but skip any state inspection — :meth:`cancel` already
        owns the ``running → failed`` transition.
        """

        cancelling = task_id in self._cancelling
        next_id: str | None = None
        async with self._lock:
            self._running.discard(task_id)
            if len(self._running) < self._cap:
                # Cheap enough to re-read on every termination — the queue is
                # small by design (one user, cap=3, typical bursts <10).
                pending = self._task_store.list_tasks(state="pending", limit=1)
                if pending:
                    next_id = pending[0].id
                    self._running.add(next_id)
            if cancelling:
                self._cancelling.discard(task_id)
        if next_id is not None:
            await self._activate(next_id)
            _logger.info(
                "task_scheduler.promoted_after_termination",
                terminated_task_id=task_id,
                promoted_task_id=next_id,
            )

    async def resume(self, task_id: str) -> None:
        """Re-enqueue a task that was paused in ``waiting_input``.

        Called by the orchestrator's ``forward_to_subtask`` path right after
        appending the user's reply to the task's message log. Behaves like
        :meth:`enqueue` but transitions ``waiting_input → running`` (instead
        of ``pending → running``). When the running cap is saturated the
        task is left in ``waiting_input`` and the resume is silently dropped
        — for the slice scope cap=3 with all running is a deadlock scenario
        we accept (the user will see no progress until a slot frees, and
        :meth:`on_task_terminated` will not promote a waiting_input task by
        itself).
        """

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.warning("task_scheduler.resume_unknown_task", task_id=task_id)
            return
        if task.state != "waiting_input":
            _logger.warning(
                "task_scheduler.resume_wrong_state",
                task_id=task_id,
                state=task.state,
            )
            return

        async with self._lock:
            if len(self._running) >= self._cap:
                _logger.warning(
                    "task_scheduler.resume_no_slot",
                    task_id=task_id,
                    running=len(self._running),
                    cap=self._cap,
                )
                return
            self._running.add(task_id)
        await self._activate(task_id)

    async def recover_after_restart(self) -> None:
        """Boot-time fixup: coerce stale ``running`` rows then re-enqueue.

        A task in ``running`` at boot can only mean the previous process
        crashed mid-run (the runner can no longer be observing it). Coerce
        such rows back to ``pending`` via raw SQL — bypassing the normal
        ``update_state`` validator because ``running → pending`` is not a
        legal user transition. Then walk ``pending`` tasks in creation order
        and re-enqueue them so the cap is honoured exactly as at runtime.
        """

        # Raw SQL: state-machine validator would refuse running → pending.
        # Safe here because we own the boot phase: no runner is observing
        # any of these tasks any more. Access through the TaskStore's
        # private connection is intentional — this is a one-time boot fixup
        # so we colocate it with the scheduler that owns the running set.
        with self._task_store._lock, self._task_store._conn:
            self._task_store._conn.execute(
                "UPDATE tasks SET state = 'pending', updated_at = datetime('now')"
                " WHERE state = 'running'"
            )

        pending = self._task_store.list_tasks(state="pending")
        for task in pending:
            await self.enqueue(task.id)

    async def cancel(self, task_id: str, *, reason: str = "user_cancelled") -> None:
        """Cancel a task (slice #0023) regardless of its current state.

        Three paths depending on current state:

        - ``done`` / ``failed``: silent no-op. A request to cancel an
          already-terminal task is benign — the UI may double-fire on
          slow networks, or Jarvis may issue a cancel for a task that
          finished in between.
        - ``pending`` / ``waiting_input``: no asyncio runner is observing
          the task. We just transition to ``failed`` (legal from both
          source states), persist ``reason`` in ``task.result``, and emit
          ``task_updated`` + ``task_result``. ``pending`` rows do not
          occupy a slot in :attr:`_running` so promotion is a no-op; for
          ``waiting_input`` the slot was already freed when the runner
          returned.
        - ``running``: real :func:`asyncio.Task.cancel`. The runner's
          ``await self._client.chat(...)`` (or any other await point) will
          raise :class:`asyncio.CancelledError`. We mark the task as being
          cancelled in :attr:`_cancelling` so the runner's done-callback
          does not race us — the callback then only handles slot bookkeeping
          and pending-promotion. Once the runner has actually returned we
          persist the failed state + reason ourselves.

        Cancellation of an unknown ``task_id`` logs a warning and returns
        without raising.
        """

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.warning("task_scheduler.cancel_unknown_task", task_id=task_id)
            return

        if task.state in ("done", "failed"):
            _logger.info(
                "task_scheduler.cancel_already_terminal",
                task_id=task_id,
                state=task.state,
            )
            return

        # Slice 0039: surface the cancellation as a task-category debug event
        # before we proceed with the state-specific cleanup.
        emit_debug(
            category="task",
            severity="info",
            source="bob.task_scheduler.cancel",
            summary=f"Sub-task '{task.title}' annulée",
            payload={
                "task_id": task_id,
                "title": task.title,
                "reason": reason,
                "previous_state": task.state,
            },
        )

        if task.state in ("pending", "waiting_input"):
            # No asyncio task to cancel — just persist the failure state.
            await self._finalize_cancelled(task_id, reason=reason)
            # ``pending`` did not occupy a slot; ``waiting_input`` already
            # freed it. Either way nothing to promote here — the cap was
            # not reduced by this cancellation.
            return

        # state == "running" — cancel the asyncio task driving the runner.
        runner_task = self._runners.get(task_id)
        # Mark before cancelling so the done-callback (which fires
        # synchronously when ``runner_task`` resolves) sees the flag.
        self._cancelling.add(task_id)

        if runner_task is None:
            # In-memory state out of sync with the SQL row (task says
            # ``running`` but we never registered a runner). Defensive only:
            # the regular code paths always populate ``self._runners`` before
            # transitioning. Treat it like a pending cancel.
            _logger.warning(
                "task_scheduler.cancel_missing_runner",
                task_id=task_id,
            )
            await self._finalize_cancelled(task_id, reason=reason)
            await self._free_slot_and_promote(task_id)
            return

        if not runner_task.done():
            # Issue 0045 — cooperative cancel before hard kill. If the boot
            # wired a cooperative-cancel callback for this task (which
            # :meth:`SubAgentRunner.request_cancel` populates), invoke it
            # and wait up to ``cancel_grace_seconds`` for the runner to
            # reach its next checkpoint and terminate cleanly. Past the
            # grace we escalate via :meth:`asyncio.Task.cancel`. The
            # runner converts the resulting :class:`asyncio.CancelledError`
            # into a forced ``done(cancelled, hard_killed)`` — see
            # :mod:`bob.sub_agent.runner` docstring.
            coop_cancel: Callable[[], None] | None = None
            if self._coop_cancel_factory is not None:
                coop_cancel = self._coop_cancel_factory(task_id)
            if coop_cancel is not None:
                coop_cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._await_runner(runner_task)),
                        timeout=self._cancel_grace_seconds,
                    )
                except TimeoutError:
                    _logger.warning(
                        "task_scheduler.coop_cancel_grace_elapsed",
                        task_id=task_id,
                        grace_seconds=self._cancel_grace_seconds,
                    )
            if not runner_task.done():
                runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner_task

        # The done-callback fires when ``runner_task`` resolves. It checks
        # ``self._cancelling`` and only frees the slot + promotes a pending
        # row — it does NOT re-transition the state. Persist the failed
        # state + reason here so the caller (WS handler or Jarvis tool) can
        # rely on the state being settled by the time ``cancel`` returns.
        await self._finalize_cancelled(task_id, reason=reason)

    @staticmethod
    async def _await_runner(runner_task: asyncio.Task[None]) -> None:
        """Await ``runner_task`` swallowing any exception.

        Used by the cooperative-cancel path so :func:`asyncio.wait_for`
        sees a clean completion (or its own ``TimeoutError``) instead of
        a leaked ``CancelledError`` from the runner.
        """

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner_task

    # --- Internals -----------------------------------------------------------

    async def _activate(self, task_id: str) -> None:
        """Transition ``task_id`` to ``running``, emit, schedule the runner.

        Called outside :attr:`_lock` once the in-memory slot has been
        reserved. Failures (invalid transition, missing row) release the
        slot so a single bad task cannot stall the whole queue.
        """

        try:
            self._task_store.update_state(task_id, "running")
        except TaskStoreError:
            _logger.exception(
                "task_scheduler.activate_transition_failed",
                task_id=task_id,
            )
            await self._release_slot(task_id)
            return

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception(
                "task_scheduler.activate_reload_failed",
                task_id=task_id,
            )
            await self._release_slot(task_id)
            return

        emit_debug(
            category="task",
            severity="info",
            source="bob.task_scheduler._activate",
            summary=f"Sub-task '{task.title}' démarre",
            payload={
                "task_id": task_id,
                "title": task.title,
                "running": len(self._running),
                "cap": self._cap,
            },
        )

        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": task.state,
                "needs_attention": task.needs_attention,
                "updated_at": task.updated_at,
            }
        )

        # Slice 0039: the runner coroutine inherits the ``current_turn_id``
        # ContextVar from the calling context. When ``_activate`` is invoked
        # synchronously from inside ``Orchestrator.process_user_message``, the
        # sub-task's ``llm`` / ``task`` events stay grouped under the parent
        # turn — that's the whole point of the ContextVar propagation slice
        # 0039 wires. The follow-up promotion path (``on_task_terminated``)
        # runs outside the original turn and gets a fresh None ``turn_id``,
        # which is the correct behaviour for a tail-of-queue runner.
        #
        # Issue 0045: route the coroutine through the shared TaskGroup when
        # the scheduler has been ``start``-ed. Fall back to
        # :func:`asyncio.create_task` for legacy callers (existing tests).
        runner_coro = self._runner_factory(task_id)
        if self._task_group is not None:
            runner_task = self._task_group.create_task(runner_coro)
        else:
            runner_task = asyncio.create_task(runner_coro)
        # Slice #0023 — track the asyncio handle so :meth:`cancel` can call
        # ``task.cancel()`` on it. Cleared in the done-callback.
        self._runners[task_id] = runner_task
        runner_task.add_done_callback(self._make_done_callback(task_id))
        _logger.info(
            "task_scheduler.promoted",
            task_id=task_id,
            running=len(self._running),
            cap=self._cap,
        )

    async def _release_slot(self, task_id: str) -> None:
        """Free the in-memory slot reserved for ``task_id`` after a failed activation."""

        async with self._lock:
            self._running.discard(task_id)

    async def _finalize_cancelled(self, task_id: str, *, reason: str) -> None:
        """Persist the failed state + reason for a cancelled task; emit events.

        Used by :meth:`cancel` for all paths (pending / waiting_input /
        running). The current state may be terminal already if the task
        finished between the cancel decision and this call — guard against
        the invalid transition. ``set_result`` is always idempotent so we
        try it regardless to capture the reason.
        """

        # Reload to learn whether the task is still cancellable. We may
        # race with the runner finishing naturally — accept the loss.
        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.warning("task_scheduler.cancel_finalize_unknown_task", task_id=task_id)
            return

        if task.state in ("done", "failed"):
            _logger.info(
                "task_scheduler.cancel_finalize_already_terminal",
                task_id=task_id,
                state=task.state,
            )
            return

        try:
            self._task_store.set_result(task_id, reason)
            self._task_store.update_state(task_id, "failed")
        except TaskStoreError:
            _logger.exception(
                "task_scheduler.cancel_finalize_failed",
                task_id=task_id,
            )
            return

        try:
            updated = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.exception(
                "task_scheduler.cancel_finalize_reload_failed",
                task_id=task_id,
            )
            return

        await ws_events.emit(
            {
                "type": "task_updated",
                "task_id": task_id,
                "state": updated.state,
                "needs_attention": updated.needs_attention,
                "updated_at": updated.updated_at,
            }
        )
        await ws_events.emit(
            {
                "type": "task_result",
                "task_id": task_id,
                "result": reason,
            }
        )

    async def _free_slot_and_promote(self, task_id: str) -> None:
        """Defensive fallback used by :meth:`cancel` when no runner is tracked.

        Mirrors the slot bookkeeping :meth:`on_task_terminated` does, but
        without any state inspection — the caller has already finalised the
        state. Should only fire in pathological "running row with no runner"
        situations.
        """

        next_id: str | None = None
        async with self._lock:
            self._running.discard(task_id)
            self._cancelling.discard(task_id)
            if len(self._running) < self._cap:
                pending = self._task_store.list_tasks(state="pending", limit=1)
                if pending:
                    next_id = pending[0].id
                    self._running.add(next_id)
        if next_id is not None:
            await self._activate(next_id)

    def _make_done_callback(self, task_id: str) -> Callable[[asyncio.Task[None]], None]:
        """Return a done-callback closure that re-enters via :meth:`on_task_terminated`.

        The callback runs synchronously on the loop thread when the runner
        task completes. We re-enter the scheduler asynchronously via
        ``create_task`` so the lock acquisition can run on the event loop.
        """

        def _callback(runner_task: asyncio.Task[None]) -> None:
            # Drop the runner handle — :meth:`cancel` no longer needs it.
            self._runners.pop(task_id, None)
            # Surface unexpected exceptions from the runner — but never raise
            # them out of the callback, that would log "Task exception was
            # never retrieved" at warning level and confuse debugging.
            # ``CancelledError`` from :meth:`cancel` is expected — don't log it.
            exc = runner_task.exception() if not runner_task.cancelled() else None
            if exc is not None:
                _logger.error(
                    "task_scheduler.runner_crashed",
                    task_id=task_id,
                    error=str(exc),
                )
            followup = asyncio.create_task(self.on_task_terminated(task_id))
            self._followups.add(followup)
            followup.add_done_callback(self._followups.discard)

        return _callback


def build_default_scheduler(
    settings: Settings,
    task_store: TaskStore,
    runner_factory: RunnerFactory,
    *,
    coop_cancel_factory: CooperativeCancelFactory | None = None,
    cancel_grace_seconds: float = DEFAULT_COOP_CANCEL_GRACE_SECONDS,
) -> TaskScheduler:
    """Build a :class:`TaskScheduler` using settings + provided dependencies.

    The ``runner_factory`` is injected because the scheduler does not depend
    on the LLM layer — the orchestrator wires its concrete
    :class:`SubAgentRunner` here at boot.

    Issue 0045: optional ``coop_cancel_factory`` lets the boot path plug a
    cooperative-cancel hook (typically :meth:`SubAgentRunner.request_cancel`)
    so :meth:`TaskScheduler.cancel` honours the 2 s grace window before
    escalating to a hard kill. Legacy callers leave it at ``None`` to keep
    slice #0023 behaviour.
    """

    return TaskScheduler(
        task_store=task_store,
        cap=settings.MAX_RUNNING_TASKS,
        runner_factory=runner_factory,
        coop_cancel_factory=coop_cancel_factory,
        cancel_grace_seconds=cancel_grace_seconds,
    )


# --- Singleton plumbing -------------------------------------------------------
#
# Mirrors :mod:`bob.task_store`. The boot path (see :mod:`bob.main`) primes
# the singleton after the TaskStore is set; tests prime it themselves when
# they need it.

_DEFAULT_SCHEDULER: TaskScheduler | None = None


def set_default_scheduler(scheduler: TaskScheduler | None) -> None:
    """Install (or clear) the process-wide singleton :class:`TaskScheduler`."""

    global _DEFAULT_SCHEDULER
    _DEFAULT_SCHEDULER = scheduler


def get_default_scheduler() -> TaskScheduler:
    """Return the process-wide singleton, raising if it hasn't been primed."""

    if _DEFAULT_SCHEDULER is None:
        raise RuntimeError(
            "TaskScheduler default singleton not initialised. Did the app lifespan (bob.main) run?"
        )
    return _DEFAULT_SCHEDULER
