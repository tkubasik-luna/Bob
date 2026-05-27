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
from bob.llm.factory import build_subagent_client
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

    # Sub-agent runner factory used by the scheduler. We build the LLM client
    # once and reuse it across every runner invocation — the underlying
    # client is stateless and the orchestrator's previous wiring did the
    # same (one client instance, many calls).
    #
    # Issue 0045: the runner is wrapped in a shared TaskGroup managed by the
    # scheduler. The boot path keeps a per-task index of live runners so the
    # cooperative-cancel hook resolves to :meth:`SubAgentRunner.request_cancel`
    # before the scheduler escalates to the hard-kill path.
    subagent_client = build_subagent_client(settings)
    subagent_policy = default_policy()
    subagent_registry = build_default_subagent_registry()
    live_runners: dict[str, SubAgentRunner] = {}

    def _runner_factory(task_id: str) -> Coroutine[Any, Any, None]:
        runner = SubAgentRunner(
            subagent_client=subagent_client,
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
    orchestrator.start_proactive_loop()
    proactivity = ProactivityHandler(orchestrator_factory=get_default_orchestrator)
    bus.subscribe("task_state_changed", proactivity.on_task_state_changed)

    load_jarvis_prompt(data_dir)

    _logger.info(
        "bob.boot",
        data_dir=str(data_dir),
        db_path=str(db_path),
        history_len=len(store.history()),
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
        await orchestrator.stop_proactive_loop()
        # Issue 0045 — drain the scheduler's TaskGroup deterministically so
        # in-flight sub-agents don't leak past the lifespan teardown.
        await scheduler.stop()
        orchestrator_module.set_default_orchestrator(None)
        set_event_bus(None)
        task_scheduler_module.set_default_scheduler(None)
        task_store_module.set_default_store(None)
        jarvis_store_module.set_default_store(None)
        conn.close()
        uninstall_file_sink()


app = FastAPI(title="Bob backend", lifespan=lifespan)
app.include_router(ws_router)
app.include_router(ws_debug_router)
app.include_router(debug_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
