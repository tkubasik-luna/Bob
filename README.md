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

Copy `.env.example` to `.env` at repo root.

Two LLM backends:

- `LLM_PROVIDER=lm_studio` (default) — OpenAI-compatible HTTP endpoint. Set `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`.
- `LLM_PROVIDER=claude_cli` — shells out to the `claude` CLI in `-p` mode. Requires `claude` on `PATH` (or `CLAUDE_CLI_BIN=/path/to/claude`). Optional `CLAUDE_CLI_MODEL` (e.g. `sonnet`, `opus`).

`scripts/dev.sh` includes an interactive provider picker (rewrites `LLM_PROVIDER` in `.env`). Non-interactive override: `BOB_PROVIDER=claude_cli ./scripts/dev.sh`, or flags `--lm` / `--claude` / `--provider=<value>`.
