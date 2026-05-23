#!/usr/bin/env bash
# Lance backend + Tauri en parallèle. Ctrl+C tue les deux.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "[bob] .env absent — copie depuis .env.example"
  cp .env.example .env
fi

# Picker provider LLM. Override: BOB_PROVIDER=lm_studio|claude_cli, ou --provider=...
PROVIDER_OVERRIDE="${BOB_PROVIDER:-}"
for arg in "$@"; do
  case "$arg" in
    --provider=*) PROVIDER_OVERRIDE="${arg#--provider=}" ;;
    --lm) PROVIDER_OVERRIDE="lm_studio" ;;
    --claude) PROVIDER_OVERRIDE="claude_cli" ;;
  esac
done

if [[ -z "$PROVIDER_OVERRIDE" && -t 0 && -t 1 ]]; then
  current="$(grep -E '^LLM_PROVIDER=' .env | head -1 | cut -d= -f2- || true)"
  current="${current:-lm_studio}"
  echo "[bob] Provider LLM ? (actuel: $current)"
  echo "  1) lm_studio  (LM Studio / OpenAI-compatible)"
  echo "  2) claude_cli (claude -p)"
  echo "  Entrée = garder $current"
  read -r -p "> " choice
  case "$choice" in
    1|lm|lm_studio)   PROVIDER_OVERRIDE="lm_studio" ;;
    2|cl|claude|claude_cli) PROVIDER_OVERRIDE="claude_cli" ;;
    "") PROVIDER_OVERRIDE="$current" ;;
    *)  echo "[bob] choix '$choice' inconnu, garde $current"; PROVIDER_OVERRIDE="$current" ;;
  esac
fi

if [[ -n "$PROVIDER_OVERRIDE" ]]; then
  case "$PROVIDER_OVERRIDE" in
    lm_studio|claude_cli) ;;
    *) echo "[bob] LLM_PROVIDER invalide: $PROVIDER_OVERRIDE" >&2; exit 1 ;;
  esac
  if grep -qE '^LLM_PROVIDER=' .env; then
    # macOS-compatible in-place sed
    sed -i.bak -E "s|^LLM_PROVIDER=.*|LLM_PROVIDER=$PROVIDER_OVERRIDE|" .env && rm -f .env.bak
  else
    printf '\nLLM_PROVIDER=%s\n' "$PROVIDER_OVERRIDE" >> .env
  fi
  export LLM_PROVIDER="$PROVIDER_OVERRIDE"
  echo "[bob] LLM_PROVIDER=$PROVIDER_OVERRIDE"
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
  exec uv run uvicorn bob.main:app --reload --host "$HOST" --port "$PORT"
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
