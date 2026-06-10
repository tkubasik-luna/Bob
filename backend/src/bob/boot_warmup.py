"""BootWarmup — preloads in a supervised background task (PRD 0018 / issue 0129).

Before this module the lifespan AWAITED the Kokoro preload + warmup before
yielding: a cold boot (model download, espeak-ng G2P warmup) blocked the whole
backend for 30 s+ — no WS connection, no HUD, nothing — to pay a cost only the
*first synthesis* needs. The STT whisper download and the per-role LM Studio
model loads were not prepaid at all, so the first voice turn after boot paid
them inline.

:class:`BootWarmup` moves every preload into named *steps*, each spawned as a
supervised background task (issue 0124) when the lifespan calls
:meth:`BootWarmup.start` just before yielding:

- ``stt`` — whisper model download + load (:meth:`bob.stt_engine.SttEngine.preload`);
- ``tts`` — Kokoro preload + tiny warmup synthesis
  (:meth:`bob.tts_service.KokoroTtsService.preload` / ``warmup``);
- ``llm_roles`` — the voice roles' LM Studio models (Jarvis, Thinker, Draft)
  made resident via the per-host :meth:`bob.lm_studio_manager.LMStudioManager.reconcile`
  pass (Annexe J steps 3-5), so the first turn's inference never JIT-loads.

Steps run CONCURRENTLY (each is its own task; the blocking work happens in
worker threads), so a slow whisper download never delays the Kokoro warmup.

A user who beats the warmup is safe by construction: the engines' internal
load locks serialize the per-turn lazy-load paths against the warmup thread,
and the EXISTING "preparing" surfaces fire exactly as before — ``stt_preparing``
/ ``stt_ready`` from :meth:`bob.voice_turn.VoiceTurn.start`, ``tts_preparing``
/ ``tts_ready`` from :func:`bob.ws_router._synthesize_and_stream`. A turn
arriving mid-warmup therefore awaits only the not-yet-ready remainder.

Failures: a step that raises records ``"Type: msg"`` in :attr:`BootWarmup.errors`
(surfaced by ``/health`` as ``warmup_errors`` → ``status: degraded``) and
re-raises so the issue-0124 supervisor produces the loud ERROR log + ``system``
debug event. Boot is never taken down — the steps run strictly after the
lifespan yielded. A cancelled step (shutdown) records nothing.

The existing skip-preload setting (``BOB_SKIP_TTS_PRELOAD``, set by the attest
harness / CI) is honoured by the lifespan simply never calling :meth:`start` —
the warmup is a no-op and every model keeps lazy-loading on first use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import structlog

from bob import stt_engine as stt_engine_module
from bob import tts_service as tts_service_module
from bob.config import Settings
from bob.llm_selection_store import RoleSelection
from bob.llm_swap import RoleManagerRegistry
from bob.lm_studio_manager import LMStudioManager
from bob.stt_engine import SttEngineUnavailableError
from bob.task_supervisor import create_supervised_task

_logger = structlog.get_logger(__name__)

#: The voice-path LLM roles warmed at boot (PRD 0018 Module 8). ``subagent``
#: is deliberately excluded: it serves background tasks, not the first voice
#: turn, and its model loads lazily on the first delegation exactly as before.
VOICE_ROLES: tuple[str, ...] = ("jarvis", "thinker", "draft")


@dataclass(frozen=True)
class WarmupStep:
    """One named unit of boot warmup work.

    ``name`` keys the step in logs, the supervised task name
    (``boot.warmup.<name>``) and the ``/health`` ``warmup_errors`` map. ``run``
    is the async body; blocking work must hop to a thread itself (the step
    factories below all do).
    """

    name: str
    run: Callable[[], Awaitable[None]]


class BootWarmup:
    """Run :class:`WarmupStep`\\ s as supervised background tasks.

    Construct with the steps, then :meth:`start` once (idempotent) AFTER the
    point boot must not block — each step becomes its own
    :func:`bob.task_supervisor.create_supervised_task`, so a step failure is
    loudly logged + surfaced on ``/ws/debug`` (issue 0124) and additionally
    recorded in :attr:`errors` for the ``/health`` endpoint. :meth:`stop`
    cancels whatever is still pending (lifespan teardown); :meth:`wait` is the
    test/diagnostic barrier that drains every step without raising.
    """

    def __init__(self, steps: Sequence[WarmupStep]) -> None:
        self._steps = list(steps)
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        #: ``step name -> "ExcType: message"`` for every FAILED step. Read by
        #: ``/health`` (``warmup_errors``). Cancellation records nothing.
        self.errors: dict[str, str] = {}

    @property
    def started(self) -> bool:
        """True once :meth:`start` spawned the step tasks (skip → stays False)."""

        return self._started

    @property
    def running(self) -> bool:
        """True while at least one step task has not finished."""

        return any(not task.done() for task in self._tasks)

    def start(self) -> None:
        """Spawn every step as a supervised background task. Idempotent."""

        if self._started:
            return
        self._started = True
        _logger.info("boot_warmup.begin", steps=[step.name for step in self._steps])
        for step in self._steps:
            self._tasks.append(
                create_supervised_task(
                    self._run_step(step),
                    name=f"boot.warmup.{step.name}",
                    context={"step": step.name},
                )
            )

    async def wait(self) -> None:
        """Await every spawned step; never raises (failures are in :attr:`errors`)."""

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Cancel pending steps and reap the tasks (lifespan teardown).

        A step parked in ``asyncio.to_thread`` is released immediately (the
        await is cancellable); its worker thread finishes on its own — same
        abandon-the-thread contract as the bounded TTS preload (issue 0126).
        """

        for task in self._tasks:
            task.cancel()
        await self.wait()

    async def _run_step(self, step: WarmupStep) -> None:
        _logger.info("boot_warmup.step.begin", step=step.name)
        try:
            await step.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Health surface FIRST, then re-raise so the issue-0124 supervisor
            # does the loud ERROR log + system debug event for this task.
            self.errors[step.name] = f"{type(exc).__name__}: {exc}"
            raise
        _logger.info("boot_warmup.step.done", step=step.name)


# --- step factories -----------------------------------------------------------


def stt_warmup_step(settings: Settings) -> WarmupStep:
    """Whisper download + load, off the first ``voice_start``.

    Resolves the process-wide engine at RUN time (not build time) so a test
    seam installed via :func:`bob.stt_engine.set_default_stt_engine` is
    honoured. ``STT_ENABLED=false`` skips entirely (mic frames are refused
    anyway). A missing native binding (``pywhispercpp`` is an optional extra —
    the backend is designed to boot green without it) is a logged SKIP, not a
    health failure: the voice path already degrades per turn at session open.
    """

    async def _run() -> None:
        if not settings.STT_ENABLED:
            _logger.info("boot_warmup.stt.disabled")
            return
        engine = stt_engine_module.get_default_stt_engine()
        try:
            await asyncio.to_thread(engine.preload)
        except SttEngineUnavailableError as exc:
            _logger.warning("boot_warmup.stt.unavailable", error=str(exc))

    return WarmupStep(name="stt", run=_run)


def tts_warmup_step(settings: Settings) -> WarmupStep:
    """Kokoro preload (download + pipeline load) then the tiny warmup synthesis.

    Exactly the work the pre-0129 lifespan awaited inline — now backgrounded.
    The service singleton is resolved at RUN time so tests can substitute
    :func:`bob.tts_service.get_default_tts_service`. ``settings`` is accepted
    for signature symmetry with the other factories (the service reads its own).
    """

    del settings  # the service singleton carries its own settings

    async def _run() -> None:
        tts = tts_service_module.get_default_tts_service()
        await asyncio.to_thread(tts.preload)
        await asyncio.to_thread(tts.warmup)

    return WarmupStep(name="tts", run=_run)


def llm_roles_warmup_step(
    settings: Settings,
    role_selection: RoleSelection,
    manager_registry: RoleManagerRegistry,
    *,
    roles: Sequence[str] = VOICE_ROLES,
) -> WarmupStep:
    """Make the voice roles' LM Studio models resident before the first turn.

    Groups the ``lm_studio`` roles among ``roles`` by their host (a role pins
    its own ``base_url``; ``None`` falls back to ``settings.LLM_BASE_URL``) and
    runs one :meth:`bob.lm_studio_manager.LMStudioManager.reconcile` pass per
    host in a worker thread — the budget-checked, ref-counted boot load
    sequence of Annexe J. The SAME :class:`RoleManagerRegistry` the per-role
    swap uses is threaded through, so the warmup's ref-counts and a later
    ``PUT /api/llm/roles/{role}`` agree.

    A role that is not ``lm_studio`` or has no pinned model is skipped (a
    ``claude_cli`` role has nothing to load). A role ``reconcile`` reports
    offline (unreachable host, budget refusal, OOM) is loudly WARN-logged but
    NOT a health failure: an LM Studio server that is simply off at boot must
    not stick ``/health`` to ``degraded`` — the role still lazy-loads on first
    inference exactly as before, mirroring the cold-start resolution's
    best-effort contract. Only an unexpected exception fails the step.
    """

    async def _run() -> None:
        per_host: dict[str, tuple[LMStudioManager, dict[str, tuple[str | None, int | None]]]] = {}
        for role in roles:
            selection = role_selection.role(role)
            if selection.provider != "lm_studio" or not selection.lm_model:
                _logger.info(
                    "boot_warmup.llm.skip",
                    role=role,
                    provider=selection.provider,
                    lm_model=selection.lm_model,
                )
                continue
            manager = manager_registry.for_base_url(selection.base_url or settings.LLM_BASE_URL)
            _, role_models = per_host.setdefault(manager.host, (manager, {}))
            role_models[role] = (
                selection.lm_model,
                selection.context_length.get(selection.lm_model),
            )
        for manager, role_models in per_host.values():
            results = await asyncio.to_thread(manager.reconcile, role_models)
            for result in results:
                if result.ready:
                    _logger.info(
                        "boot_warmup.llm.ready",
                        host=manager.host,
                        role=result.role,
                        model=result.model_id,
                    )
                else:
                    _logger.warning(
                        "boot_warmup.llm.offline",
                        host=manager.host,
                        role=result.role,
                        model=result.model_id,
                        detail=result.detail,
                    )

    return WarmupStep(name="llm_roles", run=_run)


def default_warmup_steps(
    settings: Settings,
    role_selection: RoleSelection,
    manager_registry: RoleManagerRegistry,
) -> list[WarmupStep]:
    """The production step set, in the order the lifespan wires them."""

    return [
        stt_warmup_step(settings),
        tts_warmup_step(settings),
        llm_roles_warmup_step(settings, role_selection, manager_registry),
    ]


__all__ = [
    "VOICE_ROLES",
    "BootWarmup",
    "WarmupStep",
    "default_warmup_steps",
    "llm_roles_warmup_step",
    "stt_warmup_step",
    "tts_warmup_step",
]
