"""FastAPI app entrypoint.

Lifespan wiring (boot, in order):

1. Configure structured logging.
2. Ensure ``BOB_DATA_DIR`` exists; open SQLite at ``{BOB_DATA_DIR}/bob.db`` and
   run all pending migrations. Prime the :class:`bob.jarvis_store.JarvisStore`
   singleton + bootstrap the ``jarvis.md`` personality file.
3. Preload + warm the Kokoro TTS pipeline so the first user message is fast.

Shutdown: release the SQLite connection.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from bob import jarvis_store as jarvis_store_module
from bob.config import get_settings
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.debug_router import router as debug_router
from bob.jarvis_prompt_loader import load_jarvis_prompt
from bob.jarvis_store import JarvisStore
from bob.logging_setup import configure_logging
from bob.tts_service import get_default_tts_service
from bob.ws_router import router as ws_router

configure_logging()
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

    db_path = data_dir / _DB_FILENAME
    conn = _open_database(str(db_path))
    apply_migrations(conn, default_migrations_dir())

    store = JarvisStore(conn)
    jarvis_store_module.set_default_store(store)

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
        jarvis_store_module.set_default_store(None)
        conn.close()


app = FastAPI(title="Bob backend", lifespan=lifespan)
app.include_router(ws_router)
app.include_router(debug_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
