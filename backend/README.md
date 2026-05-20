# Bob backend

FastAPI + WebSocket backend for the Bob personal AI assistant.

## Run

```
uv sync
uv run uvicorn bob.main:app --reload --host 127.0.0.1 --port 8000
```

## Checks

```
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```
