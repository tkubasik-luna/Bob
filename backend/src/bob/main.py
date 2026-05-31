"""FastAPI app entrypoint.

Lifespan wiring (boot, in order):

1. Configure structured logging.
2. Ensure ``BOB_DATA_DIR`` exists; open SQLite at ``{BOB_DATA_DIR}/bob.db`` and
   run all pending migrations. Prime the :class:`bob.jarvis_store.JarvisStore`
   singleton + bootstrap the ``jarvis.md`` personality file.
3. Build the :class:`TaskScheduler`, install it as the singleton and run
   ``recover_after_restart`` so any task left in ``running`` by a previous
   process is coerced back to ``pending`` then re-promoted under the cap.
4. Preload + warm the Kokoro TTS pipeline so the first user message is fast.

Shutdown: release the SQLite connection.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bob import jarvis_store as jarvis_store_module
from bob import orchestrator as orchestrator_module
from bob import task_scheduler as task_scheduler_module
from bob import task_store as task_store_module
from bob.config import get_settings
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.debug_log import (
    install_file_sink,
    install_structlog_bridge,
    uninstall_file_sink,
)
from bob.debug_router import router as debug_router
from bob.event_bus import EventBus, set_event_bus
from bob.jarvis_prompt_loader import load_jarvis_prompt
from bob.jarvis_store import JarvisStore
from bob.llm.factory import build_jarvis_client, build_subagent_client
from bob.llm_router import router as llm_router
from bob.llm_router import set_switcher as set_llm_switcher
from bob.llm_selection_store import (
    LLM_SELECTION_FILENAME,
    LLMSelection,
    LLMSelectionStore,
)
from bob.llm_selection_store import (
    set_default_store as set_default_llm_selection_store,
)
from bob.llm_swap import (
    LLMSwitcher,
    SubAgentClientHolder,
    resolve_cold_start_model,
)
from bob.lm_studio_manager import LMStudioManager
from bob.logging_setup import configure_logging
from bob.orchestrator import get_default_orchestrator
from bob.proactivity_handler import ProactivityHandler
from bob.scheduler_policy import SchedulerPolicy
from bob.sub_agent import (
    AddendumQueue,
    SubAgentRunner,
    build_default_subagent_registry,
    default_policy,
)
from bob.task_scheduler import build_default_scheduler
from bob.task_store import TaskStore
from bob.tts_service import get_default_tts_service
from bob.ws_debug import router as ws_debug_router
from bob.ws_router import router as ws_router

configure_logging()
# Slice 0039: install the structlog → debug_log bridge once at import time so
# WARN/ERROR records from any ``bob.*`` logger are auto-forwarded to the
# debug feed as ``system`` events. The handler is idempotent — re-importing
# this module (test client startup, dev reload) is safe.
install_structlog_bridge()
_logger = structlog.get_logger(__name__)

_DB_FILENAME = "bob.db"


def _open_database(db_path: str) -> sqlite3.Connection:
    """Open the SQLite connection used by the Jarvis singleton.

    ``check_same_thread=False`` is required because FastAPI runs request
    handlers in a thread pool when called from sync code (TestClient).
    """

    return sqlite3.connect(db_path, check_same_thread=False)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    data_dir = settings.BOB_DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)

    # Durable orchestration trace: tee every debug event to a JSONL file so the
    # full task lifecycle can be inspected offline (the WS feed is live-only and
    # the ring buffer is bounded + in-memory). Installed early so boot-time
    # events are captured; torn down in the finally below.
    if settings.ORCHESTRATION_LOG_ENABLED:
        install_file_sink("logs/orchestration.jsonl")

    db_path = data_dir / _DB_FILENAME
    if settings.BOB_CLEAR_ON_START and db_path.exists():
        db_path.unlink()
        _logger.info("bob.cache_cleared", db_path=str(db_path))
    conn = _open_database(str(db_path))
    apply_migrations(conn, default_migrations_dir())

    store = JarvisStore(conn)
    jarvis_store_module.set_default_store(store)
    task_store = TaskStore(conn)
    task_store_module.set_default_store(task_store)

    # LLM selection store (PRD 0012 / issue 0078). JSON file under BOB_DATA_DIR
    # owns the runtime selection: seed from .env on first boot, JSON wins after.
    llm_selection_store = LLMSelectionStore(data_dir / LLM_SELECTION_FILENAME)
    seeded_selection = llm_selection_store.seed_from_settings(settings)
    set_default_llm_selection_store(llm_selection_store)

    # LM Studio management manager (PRD 0012 / issue 0079-0080). Single boundary
    # onto the ``lmstudio`` SDK for cold-start resolution + the live model swap.
    lm_studio_manager = LMStudioManager()

    # Cold-start resolution (issue 0080): provider is LM Studio with no model
    # pinned anywhere → adopt the model already loaded in LM Studio if any, else
    # the first chat-capable downloaded model. Best-effort: an unreachable
    # server leaves the selection unpinned (never crashes boot).
    if not seeded_selection.lm_model:
        resolved = resolve_cold_start_model(seeded_selection, lm_studio_manager)
        if resolved:
            seeded_selection = LLMSelection(
                provider=seeded_selection.provider,
                lm_model=resolved,
                context_length=seeded_selection.context_length,
            )
            llm_selection_store.write(seeded_selection)
            _logger.info("bob.llm.cold_start_resolved", lm_model=resolved)

    # Sub-agent runner factory used by the scheduler. We build the LLM client
    # once and reuse it across every runner invocation — the underlying
    # client is stateless and the orchestrator's previous wiring did the
    # same (one client instance, many calls).
    #
    # Issue 0045: the runner is wrapped in a shared TaskGroup managed by the
    # scheduler. The boot path keeps a per-task index of live runners so the
    # cooperative-cancel hook resolves to :meth:`SubAgentRunner.request_cancel`
    # before the scheduler escalates to the hard-kill path.
    # Issue 0080 — the sub-agent client lives behind a MUTABLE holder so the
    # live model swap can replace it without rebuilding the scheduler. The
    # runner factory reads ``holder.client`` PER TASK: a task spawned after a
    # swap gets the new client; one already running keeps the one it captured.
    subagent_holder = SubAgentClientHolder(build_subagent_client(settings, seeded_selection))
    subagent_policy = default_policy()
    subagent_registry = build_default_subagent_registry()
    live_runners: dict[str, SubAgentRunner] = {}

    def _runner_factory(task_id: str) -> Coroutine[Any, Any, None]:
        runner = SubAgentRunner(
            subagent_client=subagent_holder.client,
            task_store=task_store,
            policy=subagent_policy,
            tool_registry=subagent_registry,
        )
        live_runners[task_id] = runner

        async def _run_and_cleanup() -> None:
            try:
                await runner.run(task_id)
            finally:
                live_runners.pop(task_id, None)

        return _run_and_cleanup()

    def _coop_cancel_factory(task_id: str) -> Callable[[], None] | None:
        runner = live_runners.get(task_id)
        if runner is None:
            return None
        return runner.request_cancel

    def _addendum_queue_factory(task_id: str) -> AddendumQueue | None:
        """Resolve the live runner's :class:`AddendumQueue` for ``task_id``.

        PRD 0006 / issue 0050. The v2 ``addendum_task`` tool pushes
        info into the queue at this address; the runner drains it at
        the next iteration boundary.
        """

        runner = live_runners.get(task_id)
        if runner is None:
            return None
        return runner.addendum_queue

    # PRD 0006 / issue 0050 — concurrency caps (3 running, 5 queued)
    # so Jarvis can degrade with a clarifying speech when the user
    # bursts more than the scheduler can absorb.
    scheduler_policy = SchedulerPolicy(
        max_running=settings.MAX_RUNNING_TASKS,
        max_queued=5,
    )
    scheduler = build_default_scheduler(
        settings,
        task_store,
        _runner_factory,
        coop_cancel_factory=_coop_cancel_factory,
        cancel_grace_seconds=subagent_policy.cancel_grace_seconds,
        policy=scheduler_policy,
    )
    task_scheduler_module.set_default_scheduler(scheduler)
    await scheduler.start()
    await scheduler.recover_after_restart()

    # EventBus + proactivity wiring (slice #0021, #0025). The bus is
    # process-wide; the handler resolves the orchestrator each call so tests
    # that swap singletons inside the lifespan still see fresh state. Slice
    # #0025 makes the orchestrator a true singleton so its proactive queue +
    # typing flag are shared across the WS handler and the bus subscriber;
    # the flusher runs as a background task for the whole lifespan.
    bus = EventBus()
    set_event_bus(bus)
    orchestrator = get_default_orchestrator()
    # PRD 0006 / issue 0050 — late-bind the addendum factory now that
    # the live runner pool exists.
    orchestrator.set_addendum_queue_factory(_addendum_queue_factory)
    # Issue 0080 — the singleton builds its Jarvis client from frozen .env; align
    # it with the (possibly cold-start-resolved) live selection so boot and a
    # later swap agree on the active model.
    orchestrator.set_jarvis_client(build_jarvis_client(settings, seeded_selection))
    orchestrator.start_proactive_loop()
    proactivity = ProactivityHandler(orchestrator_factory=get_default_orchestrator)
    bus.subscribe("task_state_changed", proactivity.on_task_state_changed)

    # Issue 0080 — live model swap coordinator. Owns the asyncio.Lock and pushes
    # rebuilt clients into the orchestrator + sub-agent holder. Primed into the
    # router so ``PUT /api/llm/selection`` can delegate to it; cleared on teardown.
    llm_switcher = LLMSwitcher(
        settings=settings,
        manager=lm_studio_manager,
        selection_store=llm_selection_store,
        orchestrator=orchestrator,
        subagent_holder=subagent_holder,
    )
    set_llm_switcher(llm_switcher)

    load_jarvis_prompt(data_dir)

    _logger.info(
        "bob.boot",
        data_dir=str(data_dir),
        db_path=str(db_path),
        history_len=len(store.history()),
        llm_provider=seeded_selection.provider,
        llm_model=seeded_selection.lm_model,
    )

    tts = get_default_tts_service()
    _logger.info("startup.preload.kokoro.begin")
    try:
        await asyncio.to_thread(tts.preload)
        _logger.info("startup.preload.kokoro.done")
        await asyncio.to_thread(tts.warmup)
    except Exception:
        _logger.exception("startup.preload.kokoro.failed")

    try:
        yield
    finally:
        set_llm_switcher(None)
        await orchestrator.stop_proactive_loop()
        # Issue 0045 — drain the scheduler's TaskGroup deterministically so
        # in-flight sub-agents don't leak past the lifespan teardown.
        await scheduler.stop()
        orchestrator_module.set_default_orchestrator(None)
        set_event_bus(None)
        task_scheduler_module.set_default_scheduler(None)
        task_store_module.set_default_store(None)
        jarvis_store_module.set_default_store(None)
        set_default_llm_selection_store(None)
        conn.close()
        uninstall_file_sink()


app = FastAPI(title="Bob backend", lifespan=lifespan)

# PRD 0012 — the Sphere HUD webview calls the REST API (`/api/llm/*`) cross-origin
# (Vite dev server on :1420, and the Tauri webview origin in the packaged app), so
# the browser preflights mutating requests with OPTIONS. Without CORS the preflight
# 405s and the picker never reaches PUT. WS is exempt (no preflight). Allow the known
# local dev / Tauri origins only — this is a localhost-only desktop backend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(ws_router)
app.include_router(ws_debug_router)
app.include_router(debug_router)
app.include_router(llm_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
