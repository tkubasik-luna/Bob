"""FastAPI app entrypoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from bob.debug_router import router as debug_router
from bob.logging_setup import configure_logging
from bob.tts_service import get_default_tts_service
from bob.ws_router import router as ws_router

configure_logging()

_logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Preload + warm the Kokoro pipeline so the first user message is fast.

    Two steps:

    1. ``preload`` — instantiate KPipeline (HF snapshot mmap + PyTorch model
       load). Costs a few seconds on cold start.
    2. ``warmup`` — run one tiny synthesis to JIT the graph and prime the
       voice tensor cache, so the very first user message hits a hot path
       instead of paying the graph-capture stall.
    """

    tts = get_default_tts_service()
    _logger.info("startup.preload.kokoro.begin")
    try:
        await asyncio.to_thread(tts.preload)
        _logger.info("startup.preload.kokoro.done")
        await asyncio.to_thread(tts.warmup)
    except Exception:
        # Don't crash the app — TTS will retry lazily on first request and the
        # ws_router will surface the failure as an audio_error to the client.
        _logger.exception("startup.preload.kokoro.failed")
    yield


app = FastAPI(title="Bob backend", lifespan=_lifespan)
app.include_router(ws_router)
app.include_router(debug_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
