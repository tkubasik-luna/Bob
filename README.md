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

- `LLM_PROVIDER=lm_studio` (default) — OpenAI-compatible HTTP endpoint. Set `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`. The LM Studio model must support function calling (e.g. Qwen2.5, Llama 3.1 Instruct) for the `LLMClient.complete()` tool-calling API.
- `LLM_PROVIDER=claude_cli` — shells out to the `claude` CLI in `-p` mode. Requires `claude` on `PATH` (or `CLAUDE_CLI_BIN=/path/to/claude`). Optional `CLAUDE_CLI_MODEL` (e.g. `sonnet`, `opus`).

`scripts/dev.sh` includes an interactive provider picker (rewrites `LLM_PROVIDER` in `.env`). Non-interactive override: `BOB_PROVIDER=claude_cli ./scripts/dev.sh`, or flags `--lm` / `--claude` / `--provider=<value>`.

## Gmail connector

The `bob.connectors.gmail` package adds read-only Gmail access (PRD 0007). Setup is a one-shot OAuth consent flow against the user's own GCP project — Bob never sees a Google API key, only the per-user refresh token persisted under `~/.bob/gmail/`.

### One-time GCP project setup

1. Create a Google Cloud project: open the [GCP Console](https://console.cloud.google.com/projectcreate), name it (e.g. `bob-personal-assistant`), and click **Create**.
2. Enable the Gmail API: from the project dashboard go to **APIs & Services > Library**, search for "Gmail API", and click **Enable**.
3. Configure the OAuth consent screen: **APIs & Services > OAuth consent screen**. Pick **External** (unless your Google account is part of a Workspace org — then **Internal** works too). Fill in the app name (`Bob`), user support email, and developer contact email. Save and continue. On the **Scopes** step you can skip adding scopes (Bob requests `gmail.readonly` at runtime). On the **Test users** step, add your own Google account so unverified-app consent works while the project stays in test mode. You do not need to publish the app.
4. Create the OAuth client: **APIs & Services > Credentials > Create Credentials > OAuth client ID**. Application type = **Desktop app**. Name it (`Bob desktop client`). Click **Create**, then **Download JSON**.
5. Drop the downloaded file at `~/.bob/gmail/credentials.json` (create the directory if needed): `mkdir -p ~/.bob/gmail && mv ~/Downloads/client_secret_*.json ~/.bob/gmail/credentials.json`.

### Bootstrap script

With `credentials.json` in place, run the one-shot consent flow:

```
cd backend
uv run python -m bob.connectors.gmail.auth
```

A browser window opens at Google's consent screen — sign in with the same Google account you added as a test user, confirm the `gmail.readonly` scope, and approve. The script persists a refresh token at `~/.bob/gmail/token.json` (chmod 0600). Subsequent app runs reuse the cached token; expired access tokens are refreshed silently. If the refresh token is later revoked (e.g. via [myaccount.google.com](https://myaccount.google.com/permissions)), re-run the same command to get a fresh token.

### Config overrides

Override the default file paths via `.env`:

```
GMAIL_CREDENTIALS_PATH=/abs/path/to/credentials.json
GMAIL_TOKEN_PATH=/abs/path/to/token.json
```

### Scope

Bob requests only `https://www.googleapis.com/auth/gmail.readonly` — the worst-case blast radius is "read", never "delete" or "send".
