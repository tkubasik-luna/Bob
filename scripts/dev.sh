#!/usr/bin/env bash
# Lance backend + Tauri en parallèle. Ctrl+C tue les deux.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "[bob] .env absent — copie depuis .env.example"
  cp .env.example .env
fi

# Picker provider LLM. Override: BOB_PROVIDER=lm_studio|claude_cli|remote, ou --provider=...
# "remote" = OpenAI-compatible HTTP backend pointé sur une URL distante (réutilise LLM_PROVIDER=lm_studio + LLM_BASE_URL).
PROVIDER_OVERRIDE="${BOB_PROVIDER:-}"
REMOTE_URL_OVERRIDE="${BOB_REMOTE_URL:-}"
for arg in "$@"; do
  case "$arg" in
    --provider=*) PROVIDER_OVERRIDE="${arg#--provider=}" ;;
    --lm) PROVIDER_OVERRIDE="lm_studio" ;;
    --claude) PROVIDER_OVERRIDE="claude_cli" ;;
    --remote=*) PROVIDER_OVERRIDE="remote"; REMOTE_URL_OVERRIDE="${arg#--remote=}" ;;
    --remote) PROVIDER_OVERRIDE="remote" ;;
  esac
done

if [[ -z "$PROVIDER_OVERRIDE" && -t 0 && -t 1 ]]; then
  current="$(grep -E '^LLM_PROVIDER=' .env | head -1 | cut -d= -f2- || true)"
  current="${current:-lm_studio}"
  current_url="$(grep -E '^LLM_BASE_URL=' .env | head -1 | cut -d= -f2- || true)"
  echo "[bob] Provider LLM ? (actuel: $current)"
  echo "  1) lm_studio     (LM Studio local / OpenAI-compatible)"
  echo "  2) claude_cli    (claude -p)"
  echo "  3) serveur distant (OpenAI-compatible sur URL distante)"
  echo "  Entrée = garder $current"
  read -r -p "> " choice
  case "$choice" in
    1|lm|lm_studio)   PROVIDER_OVERRIDE="lm_studio" ;;
    2|cl|claude|claude_cli) PROVIDER_OVERRIDE="claude_cli" ;;
    3|r|remote|distant) PROVIDER_OVERRIDE="remote" ;;
    "") PROVIDER_OVERRIDE="$current" ;;
    *)  echo "[bob] choix '$choice' inconnu, garde $current"; PROVIDER_OVERRIDE="$current" ;;
  esac
  if [[ "$PROVIDER_OVERRIDE" == "remote" ]]; then
    echo "[bob] URL du serveur distant ? (OpenAI-compatible, suffixe '/v1' requis — ex: http://192.168.4.94:1234/v1)"
    [[ -n "$current_url" ]] && echo "  Entrée = garder $current_url"
    read -r -p "url> " REMOTE_URL_OVERRIDE
    if [[ -z "$REMOTE_URL_OVERRIDE" ]]; then
      REMOTE_URL_OVERRIDE="$current_url"
    fi
    if [[ -z "$REMOTE_URL_OVERRIDE" ]]; then
      echo "[bob] URL requise pour serveur distant" >&2; exit 1
    fi
    # Warn if /v1 suffix missing (LM Studio + most OpenAI-compatible servers expect it).
    if [[ "$REMOTE_URL_OVERRIDE" != */v1 && "$REMOTE_URL_OVERRIDE" != */v1/ ]]; then
      echo "[bob] ⚠️  URL ne se termine pas par '/v1' — LM Studio renverra 200 avec un body non-OpenAI et le backend crashera."
      read -r -p "  Ajouter '/v1' automatiquement ? [Y/n] " confirm
      case "$confirm" in
        n|N|no|non) ;;
        *) REMOTE_URL_OVERRIDE="${REMOTE_URL_OVERRIDE%/}/v1" ;;
      esac
    fi
  fi
fi

if [[ -n "$PROVIDER_OVERRIDE" ]]; then
  EFFECTIVE_PROVIDER="$PROVIDER_OVERRIDE"
  case "$PROVIDER_OVERRIDE" in
    lm_studio|claude_cli) ;;
    remote)
      if [[ -z "$REMOTE_URL_OVERRIDE" ]]; then
        echo "[bob] --remote requiert une URL (--remote=<url> ou BOB_REMOTE_URL=...)" >&2; exit 1
      fi
      EFFECTIVE_PROVIDER="lm_studio"
      ;;
    *) echo "[bob] LLM_PROVIDER invalide: $PROVIDER_OVERRIDE" >&2; exit 1 ;;
  esac
  if grep -qE '^LLM_PROVIDER=' .env; then
    # macOS-compatible in-place sed
    sed -i.bak -E "s|^LLM_PROVIDER=.*|LLM_PROVIDER=$EFFECTIVE_PROVIDER|" .env && rm -f .env.bak
  else
    printf '\nLLM_PROVIDER=%s\n' "$EFFECTIVE_PROVIDER" >> .env
  fi
  export LLM_PROVIDER="$EFFECTIVE_PROVIDER"
  echo "[bob] LLM_PROVIDER=$EFFECTIVE_PROVIDER"
  if [[ -n "$REMOTE_URL_OVERRIDE" ]]; then
    if grep -qE '^LLM_BASE_URL=' .env; then
      # Use | as sed delimiter to tolerate / in URLs
      sed -i.bak -E "s|^LLM_BASE_URL=.*|LLM_BASE_URL=$REMOTE_URL_OVERRIDE|" .env && rm -f .env.bak
    else
      printf '\nLLM_BASE_URL=%s\n' "$REMOTE_URL_OVERRIDE" >> .env
    fi
    export LLM_BASE_URL="$REMOTE_URL_OVERRIDE"
    echo "[bob] LLM_BASE_URL=$REMOTE_URL_OVERRIDE"
  fi
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

echo "[bob] frontend (Tauri — 2 fenêtres : legacy + new)"
(
  cd "$ROOT/frontend"
  pnpm install --silent
  exec pnpm tauri dev
) &
PIDS+=($!)

wait
