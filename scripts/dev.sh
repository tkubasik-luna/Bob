#!/usr/bin/env bash
# Lance backend + Tauri en parallèle. Ctrl+C tue les deux.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "[bob] .env absent — copie depuis .env.example"
  cp .env.example .env
fi

# Provider / modèle / URL LM Studio sont maintenant choisis À CHAUD depuis le
# HUD (panneau RÉGLAGES) et persistés dans {BOB_DATA_DIR}/llm_selection.json, qui
# prime sur .env après le premier boot. Plus aucune sélection interactive ici :
# .env ne sert qu'à l'amorçage initial (premier boot, fichier JSON absent).

# NE PAS `source .env` : bash retire les guillemets des valeurs JSON (ex:
# MCP_SERVERS) et `set -a` les exporterait mal-formées dans l'environnement, ce
# qui ferait planter le parsing pydantic au boot (os.environ prime sur .env).
# Le backend lit .env directement (pydantic-settings) ; ce script n'a besoin que
# de l'hôte/port, qu'on extrait ligne à ligne. Les overrides provider/URL sont
# déjà exportés plus haut.
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }

HOST="${BACKEND_HOST:-$(env_value BACKEND_HOST)}"
PORT="${BACKEND_PORT:-$(env_value BACKEND_PORT)}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# Extras STT à installer. ``stt`` (whisper.cpp) est le défaut, toujours posé.
# Le moteur ``sherpa`` (transducer true-streaming) tire un wheel natif séparé
# (extra ``stt-sherpa``) — on ne le pose QUE si .env sélectionne STT_ENGINE=sherpa,
# pour garder l'install légère sur le chemin whisper par défaut.
STT_ENGINE_VAL="${STT_ENGINE:-$(env_value STT_ENGINE)}"
STT_EXTRAS=(--extra stt)
if [[ "$STT_ENGINE_VAL" == "sherpa" ]]; then
  STT_EXTRAS+=(--extra stt-sherpa)
  echo "[bob] STT_ENGINE=sherpa → extra stt-sherpa (transducer true-streaming)"
fi

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
  uv sync "${STT_EXTRAS[@]}" --quiet
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

echo "[bob] frontend (Tauri — fenêtre HUD ; debug en Cmd+Shift+D)"
(
  cd "$ROOT/frontend"
  pnpm install --silent
  exec pnpm tauri dev
) &
PIDS+=($!)

wait
