"""FastAPI app entrypoint."""

from fastapi import FastAPI

app = FastAPI(title="Bob backend")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
