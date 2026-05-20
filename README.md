# Bob

Personal AI assistant. Tauri + React frontend, FastAPI backend, LM Studio (or any OpenAI-compatible endpoint) for the LLM.

## Layout

```
backend/   — Python 3.12 + FastAPI + uv
frontend/  — Tauri 2 + Vite + React + TypeScript + Tailwind v4
prd/       — Product requirement documents
issues/    — Implementation issues derived from PRDs
```

## Backend

```
cd backend
uv sync
uv run uvicorn bob.main:app --reload --host 127.0.0.1 --port 8000
```

Checks: `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy .` · `uv run pytest`

## Frontend

```
cd frontend
pnpm install
pnpm tauri dev
```

Checks: `pnpm biome check .` · `pnpm tsc --noEmit`

## Config

Copy `.env.example` to `.env` at repo root and adjust to your LM Studio setup.
