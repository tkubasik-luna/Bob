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
from bob import turn_metrics as turn_metrics_module
from bob import voice_store as voice_store_module
from bob.config import get_settings
from bob.connectors.mcp import MCPRuntime
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
from bob.llm.factory import (
    build_jarvis_role_client,
    build_role_client,
    build_subagent_role_client,
)
from bob.llm_router import reset_manager_provider as reset_llm_manager_provider
from bob.llm_router import router as llm_router
from bob.llm_router import set_manager_provider as set_llm_manager_provider
from bob.llm_router import set_role_switcher
from bob.llm_router import set_switcher as set_llm_switcher
from bob.llm_selection_store import (
    LLM_SELECTION_FILENAME,
    LLMSelection,
    LLMSelectionStore,
    RoleSelectionStore,
    set_default_role_store,
)
from bob.llm_selection_store import (
    set_default_store as set_default_llm_selection_store,
)
from bob.llm_swap import (
    LLMSwitcher,
    RoleClientRegistry,
    RoleLLMSwitcher,
    RoleManagerRegistry,
    SubAgentClientHolder,
    resolve_cold_start_model,
)
from bob.lm_studio_manager import LMStudioManager, host_from_base_url
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
from bob.voice_retention_policy import set_retention_policy as set_voice_retention_policy
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

    # Voice persistence + retention (PRD 0016 / issue 0109, Annexe E). The voice
    # store shares the Jarvis SQLite connection (the 0010/0011 migrations ran
    # above) and writes the mic/tts WAV files under ``{data_dir}/voice_audio/``.
    # The retention policy (separate size/age caps) is built from settings and
    # installed process-wide; the per-turn persist hook enforces it after each
    # write. Both cleared on teardown.
    voice_store = voice_store_module.VoiceStore(conn, data_dir)
    voice_store_module.set_default_store(voice_store)
    set_voice_retention_policy(settings.voice_retention_policy())

    # Turn latency metrics (PRD 0018 / issue 0117). The bounded in-memory
    # collector behind the per-turn ``turn_metrics`` debug event — sized from
    # settings and installed process-wide so the voice loop / orchestrator /
    # ws_router instrumentation all feed the same baseline numbers.
    turn_metrics_module.set_default_collector(
        turn_metrics_module.TurnLatencyMetrics(
            max_turns=settings.TURN_METRICS_MAX_TURNS,
            window=settings.TURN_METRICS_WINDOW,
        )
    )

    # LLM selection store (PRD 0012 / issue 0078). JSON file under BOB_DATA_DIR
    # owns the runtime selection: seed from .env on first boot, JSON wins after.
    llm_selection_store = LLMSelectionStore(data_dir / LLM_SELECTION_FILENAME)
    seeded_selection = llm_selection_store.seed_from_settings(settings)
    set_default_llm_selection_store(llm_selection_store)

    # LM Studio management manager (PRD 0012 / issue 0079-0080). Single boundary
    # onto the ``lmstudio`` SDK for cold-start resolution + the live model swap.
    # Host derives from the PERSISTED base_url (picker URL swap) when set, else
    # the frozen .env LLM_BASE_URL — so a server chosen in a prior session is
    # honoured on the next boot.
    lm_studio_manager = LMStudioManager(
        host=host_from_base_url(seeded_selection.base_url or settings.LLM_BASE_URL)
    )

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
                base_url=seeded_selection.base_url,
            )
            llm_selection_store.write(seeded_selection)
            _logger.info("bob.llm.cold_start_resolved", lm_model=resolved)

    # Per-role LLM selection store (PRD 0016 / issue 0106). Shares the same JSON
    # file as the legacy flat store; seed migrates a v1 flat file forward to the
    # four-role v2 shape and guarantees a canonical file. Wiring this default
    # singleton is what GET /api/llm/roles reads through — without it the route
    # raises and the HUD picker shows "Sélection par rôle indisponible".
    role_selection_store = RoleSelectionStore(data_dir / LLM_SELECTION_FILENAME)
    seeded_role_selection = role_selection_store.seed_from_settings(settings)
    set_default_role_store(role_selection_store)

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
    # PRD 0016 — built from the PER-ROLE store (the Réglages picker drives the
    # `subagent` role), pushed live on a per-role swap via the registry sink.
    subagent_holder = SubAgentClientHolder(
        build_subagent_role_client(seeded_role_selection, settings)
    )
    subagent_policy = default_policy()
    subagent_registry = build_default_subagent_registry()

    # MCP fleet (PRD 0015 / issue 0094). Connect every configured MCP server,
    # discover + curate + wrap its tools, and register them into the sub-agent
    # registry the runner factory reads. Gated like ``TAVILY_API_KEY``: an empty
    # manifest registers nothing and boots green; a down / absent server is
    # logged actionably and registers nothing while its peers register normally.
    # Done BEFORE any sub-agent task can run so the MCP tools are dispatchable.
    mcp_runtime = MCPRuntime(
        settings.mcp_server_configs(),
        call_timeout_seconds=settings.MCP_CALL_TIMEOUT_SECONDS,
    )
    try:
        await mcp_runtime.startup(subagent_registry)
    except Exception:
        # Defensive: registration already swallows per-server failures, but a
        # truly unexpected error must never take the boot down.
        _logger.exception("mcp.runtime.startup_failed")

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
    # Issue 0080 — the singleton builds its Jarvis client from frozen .env;
    # align it with the live selection so boot and a later swap agree.
    # PRD 0016 — built from the PER-ROLE store (the Réglages picker drives the
    # `jarvis` voice role), pushed live on a per-role swap via the registry sink.
    orchestrator.set_jarvis_client(build_jarvis_role_client(seeded_role_selection, settings))
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
    # GET /api/llm/models otherwise builds a fresh default-host manager
    # (localhost:1234). Point it at the same host-configured manager the swap
    # path uses, so the listing reaches the real LM Studio server (LLM_BASE_URL).
    set_llm_manager_provider(lambda: lm_studio_manager)

    # Issue 0106 — per-role swap coordinator behind PUT /api/llm/roles/{role}.
    # Holds the four seeded role clients in a registry (no sinks: the thinker /
    # draft consumers rebuild fresh from the persisted store each turn, so a swap
    # takes effect by persisting the new selection). Without this wiring PUT
    # returns 503 "swap coordinator not initialised".
    # Sinks push a rebuilt role client to its LIVE consumer on a swap: `jarvis`
    # → the orchestrator's Jarvis client, `subagent` → the holder the runner
    # factory reads per task. `thinker` / `draft` have no sink — their loops
    # rebuild from the per-role store at each voice session, so a swap takes
    # effect on the next session (no live-loop setter).
    role_client_registry = RoleClientRegistry(
        {
            role: build_role_client(seeded_role_selection, role, settings)
            for role in seeded_role_selection.roles
        },
        sinks={
            "jarvis": orchestrator.set_jarvis_client,
            "subagent": subagent_holder.set,
        },
    )
    # Per-host multi-load registry (issue 0107): drives the LIVE LM Studio load
    # on a role swap. Without it a selected model is only PERSISTED — never
    # loaded into LM Studio. Pre-seed the boot manager under its own host (so
    # the host already in use shares its ref-count state) and build a fresh
    # manager per other host on first use.
    role_manager_registry = RoleManagerRegistry(
        {lm_studio_manager.host: lm_studio_manager},
        factory=lambda host: LMStudioManager(host=host),
    )
    role_llm_switcher = RoleLLMSwitcher(
        settings=settings,
        selection_store=role_selection_store,
        registry=role_client_registry,
        manager_registry=role_manager_registry,
    )
    set_role_switcher(role_llm_switcher)

    load_jarvis_prompt(data_dir)

    _logger.info(
        "bob.boot",
        data_dir=str(data_dir),
        db_path=str(db_path),
        history_len=len(store.history()),
        llm_provider=seeded_selection.provider,
        llm_model=seeded_selection.lm_model,
    )

    # Issue 0098 — the attestation harness boots a headless, text-only backend
    # and sets ``BOB_SKIP_TTS_PRELOAD`` so the Kokoro download + espeak-ng G2P
    # warmup (which can be absent / native-abort in CI) never runs. Voice still
    # lazy-loads on first synthesis if it is ever requested.
    if settings.BOB_SKIP_TTS_PRELOAD:
        _logger.info("startup.preload.kokoro.skipped")
    else:
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
        reset_llm_manager_provider()
        await orchestrator.stop_proactive_loop()
        # Issue 0045 — drain the scheduler's TaskGroup deterministically so
        # in-flight sub-agents don't leak past the lifespan teardown.
        await scheduler.stop()
        # Issue 0094 — close every MCP session so no zombie subprocess survives
        # the process. Best-effort: each aclose swallows its own teardown error.
        await mcp_runtime.aclose()
        orchestrator_module.set_default_orchestrator(None)
        set_event_bus(None)
        task_scheduler_module.set_default_scheduler(None)
        task_store_module.set_default_store(None)
        jarvis_store_module.set_default_store(None)
        voice_store_module.set_default_store(None)
        turn_metrics_module.set_default_collector(None)
        set_default_llm_selection_store(None)
        set_default_role_store(None)
        set_role_switcher(None)
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
