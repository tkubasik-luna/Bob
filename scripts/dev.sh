#!/usr/bin/env bash
# Lance backend + Tauri en parallèle. Ctrl+C tue les deux.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "[bob] .env absent — copie depuis .env.example"
  cp .env.example .env
fi

# Charge .env pour le shell courant (sans écraser les vars déjà définies)
set -a
# shellcheck disable=SC1091
source .env
set +a

HOST="${BACKEND_HOST:-127.0.0.1}"
PORT="${BACKEND_PORT:-8000}"

PIDS=()
cleanup() {
  echo
  echo "[bob] arrêt..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[bob] backend → http://$HOST:$PORT"
(
  cd "$ROOT/backend"
  uv sync --quiet
  exec uv run uvicorn bob.main:app --host "$HOST" --port "$PORT"
) &
PIDS+=($!)

# Attendre que /health réponde avant de lancer Tauri
echo "[bob] attente backend..."
for _ in {1..30}; do
  if curl -sf "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    echo "[bob] backend prêt"
    break
  fi
  sleep 0.5
done

echo "[bob] frontend (Tauri)"
(
  cd "$ROOT/frontend"
  pnpm install --silent
  exec pnpm tauri dev
) &
PIDS+=($!)

wait
