## Parent

`prd/0001-bob-mvp-foundation.md`

## What to build

Finaliser la V0 : observabilité, gestion d'erreurs end-to-end, et tests d'intégration WS. Aucune nouvelle feature, mais la V0 devient utilisable en conditions réelles (LM Studio éteint, timeout, JSON cassé).

`logging_setup` backend : configurer `structlog` au démarrage. Logs JSON sur stdout au niveau `LOG_LEVEL`. Handler dédié qui écrit chaque appel LLM dans `logs/llm-{YYYY-MM-DD}.jsonl` avec : `session_id`, timestamp, `messages` envoyés au LLM (système + historique + user), réponse brute (string), `latency_ms`, `tokens_in/out` si dispo. Un fichier par jour, rotation par date.

Error handling backend :
- LM Studio injoignable (connection refused, DNS fail) → catch, émettre `{type: "error", code: "LLM_UNREACHABLE", message: "LLM provider injoignable"}` au front, ne pas crash le WS. Logué en ERROR.
- Timeout LLM (dépassement de `LLM_TIMEOUT_SECONDS`) → catch, émettre `{type: "error", code: "LLM_TIMEOUT"}`, logué en ERROR.
- Exception inattendue dans `chat_service` → catch top-level dans `ws_router`, émettre `{type: "error", code: "INTERNAL"}`, ne pas leak la stack au client. Logué en ERROR avec stack.

Error handling frontend :
- Réception `{type: "error", code, message}` → affiche un toast (composant léger maison, pas de lib) avec le message, le rouge / accent visuel selon code, auto-dismiss après 5s.
- `isWaitingResponse` reset à false sur error (sinon spinner reste bloqué).

Tests d'intégration `ws_router` via `TestClient` FastAPI (`fastapi.testclient.TestClient` ou `httpx.AsyncClient` avec ASGI) :
- Connexion émet d'abord `{type: "session", session_id}`
- Envoi `user_msg` (avec `LLMClient` mocké côté fixture) produit séquence : `thinking start`, `assistant_msg`, `thinking end`
- LLM mocké qui lève `TimeoutError` → reçoit `{type: "error", code: "LLM_TIMEOUT"}`
- LLM mocké qui lève `ConnectionError` → reçoit `{type: "error", code: "LLM_UNREACHABLE"}`
- Disconnect → `conversation.get_history(session_id)` retourne liste vide après clear (testable via injection ou inspection state)

## Acceptance criteria

- [ ] `bob.logging_setup` configure structlog au démarrage avec sortie JSON stdout
- [ ] `LOG_LEVEL` du `.env` respecté
- [ ] Chaque appel LLM produit une ligne dans `logs/llm-{YYYY-MM-DD}.jsonl` avec `session_id`, `messages`, `raw_response`, `latency_ms`
- [ ] LM Studio éteint → front reçoit `{type: "error", code: "LLM_UNREACHABLE"}` au lieu de crash
- [ ] Timeout LLM (forcer `LLM_TIMEOUT_SECONDS=1` + modèle lent ou mock) → front reçoit `{type: "error", code: "LLM_TIMEOUT"}`
- [ ] Exception interne → `{type: "error", code: "INTERNAL"}` côté front, stack loguée backend uniquement
- [ ] Front affiche un toast pour chaque error reçue, auto-dismiss 5s
- [ ] `isWaitingResponse` reset à false sur error
- [ ] Tests intégration ws_router via TestClient FastAPI couvrent : session emit, happy path séquence, timeout, connection error, disconnect cleanup
- [ ] Tests utilisent `LLMClient` mocké via override de dépendance FastAPI
- [ ] Test manuel : kill LM Studio en cours de session → toast erreur clair, reconnexion LM Studio + retry message → fonctionne à nouveau
- [ ] `ruff`, `mypy strict`, `biome`, `tsc --noEmit`, `pytest` passent

## Blocked by

- `issues/0006-wire-ws-frontend-dispatch.md`
