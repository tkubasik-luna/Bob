# Task sidebar smoke (slice #0019)

Manual end-to-end demo that exercises the WS `task_*` events + the new
right-hand sidebar in `ChatView`.

## Prerequisites

- `.env` at repo root populated with a working LLM backend (LM Studio or
  Claude CLI). See `README.md` for the variable list.
- `backend/.venv` populated via `uv sync`.
- `frontend/node_modules` populated via `pnpm install`.

## Run

```bash
# Terminal 1 — backend
cd backend
uv run uvicorn bob.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2 — frontend (Tauri shell)
cd frontend
pnpm tauri dev
```

## What to check

1. **Empty state.** First open, sidebar shows "Aucune tâche en cours" on the
   right.
2. **Live spawn.** Send a message that Jarvis decides to delegate — e.g.
   `Draft trois variantes d'un email de remerciement pour un client.`
   - A card appears in the sidebar immediately. Dot is **grey** (pending).
   - The dot transitions to **blue** (running) within ~100 ms (the
     orchestrator promotes `pending → running` synchronously after the
     `task_created` emit).
   - When the sub-agent returns `{"action": "done", …}`, the dot turns
     **green** (done). The card stays on screen — dismiss lands in #0024.
3. **Failure path.** Force a parse error by stopping LM Studio mid-flight,
   then trigger a spawn. The card flips to **red** (failed).
4. **Reload persistence.** Close and reopen the Tauri window. The sidebar
   reconstructs from `_replay_active_tasks` on the new WS connection: every
   known task appears in its last persisted state.

## Wire-level checklist

If something looks off, open the browser devtools network inspector on the
`/ws/chat` socket. Each spawn must produce, in order:

```json
{"type":"thinking","state":"start"}
{"type":"task_created","task_id":"…","state":"pending", "title":"…", "goal":"…", "created_at":"…"}
{"type":"task_updated","task_id":"…","state":"running","needs_attention":false,"updated_at":"…"}
{"type":"assistant_msg", …}
{"type":"thinking","state":"end"}
```

Then asynchronously, once the sub-agent finishes:

```json
{"type":"task_updated","task_id":"…","state":"done", …}
{"type":"task_result","task_id":"…","result":"…"}
```

## Out of scope for this slice

- Clicking a card does nothing yet — the drawer is #0024.
- No cancellation UI — that's #0023.
- `waiting_input` state is wired in the store / palette but never produced
  by the sub-agent yet (slice #0021 ships multi-turn `ask_user`).
