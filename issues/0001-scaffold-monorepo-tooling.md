## Parent

`prd/0001-bob-mvp-foundation.md`

## What to build

Mettre en place la structure monorepo `backend/` + `frontend/` avec tout l'outillage de base, prêt à recevoir du code. Les deux applications doivent démarrer (FastAPI vide qui répond `200` sur `/health`, fenêtre Tauri vide qui affiche un placeholder React). Aucune logique métier à ce stade, juste les fondations.

Backend : init `uv`, dépendances `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `structlog`, `pytest`, configuration `ruff` + `mypy` strict. Endpoint `GET /health` qui renvoie `{"status": "ok"}`. Bind `127.0.0.1` configurable via `.env`.

Frontend : scaffold Tauri 2 + Vite + React 18 + TypeScript strict avec `pnpm`. Tailwind v4 installé et fonctionnel. Biome configuré (lint+format). Zustand ajouté en dépendance. App vide affiche juste un titre "Bob".

Racine : `.gitignore` complet (Python, Node, Tauri targets, .env, logs), `README.md` minimal expliquant comment lancer les deux côtés.

## Acceptance criteria

- [ ] `backend/` contient `pyproject.toml` géré par uv, `uv sync` installe les deps
- [ ] `uv run uvicorn bob.main:app --reload` démarre le serveur sur `127.0.0.1:8000`
- [ ] `GET http://127.0.0.1:8000/health` renvoie `{"status": "ok"}`
- [ ] `uv run ruff check .` et `uv run ruff format --check .` passent
- [ ] `uv run mypy .` passe en mode strict
- [ ] `uv run pytest` exécute (peut être 0 test, mais runner OK)
- [ ] `frontend/` contient un projet Tauri 2 + Vite + React + TS fonctionnel
- [ ] `pnpm install` installe les deps
- [ ] `pnpm tauri dev` ouvre une fenêtre desktop qui affiche "Bob"
- [ ] `pnpm biome check .` passe
- [ ] `pnpm tsc --noEmit` passe en strict
- [ ] Tailwind v4 fonctionne (une classe utility appliquée est visible)
- [ ] Zustand installé (vérifiable dans `package.json`)
- [ ] `.gitignore` à la racine ignore `__pycache__/`, `.venv/`, `node_modules/`, `target/`, `dist/`, `.env`, `logs/`
- [ ] `README.md` à la racine documente commandes de lancement backend + frontend
- [ ] `.env.example` à la racine documente `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `BACKEND_HOST`, `BACKEND_PORT`, `LOG_LEVEL`

## Blocked by

None - can start immediately.
