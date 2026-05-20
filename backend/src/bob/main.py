"""FastAPI app entrypoint."""

from fastapi import FastAPI

from bob.logging_setup import configure_logging
from bob.ws_router import router as ws_router

configure_logging()

app = FastAPI(title="Bob backend")
app.include_router(ws_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
