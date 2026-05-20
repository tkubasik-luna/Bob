# PRD 0001 — Bob MVP Foundation

## Problem Statement

Tom veut construire un assistant IA personnel de type Jarvis, multi-plateforme, qui combine à terme TTS/STT, RAG sur knowledge graph, et une UI riche pilotée par le LLM. Aujourd'hui il n'a rien : pas de backend, pas de front, pas de protocole entre les deux. Avant de viser Jarvis-viz, RAG, ou self-updating memory, il faut poser des bases techniques solides — un backend agent connecté à un LLM local (LM Studio, mais interchangeable), un protocole de communication temps-réel extensible, et un front capable d'afficher des composants dynamiquement décidés par le LLM.

Sans cette fondation, chaque étape future (TTS, function calling, KG, mémoire auto-update) se construirait sur des assemblages incohérents et incompatibles.

## Solution

Livrer une V0 ultra-ciblée : un chat texte fonctionnel de bout en bout, mais structuré pour absorber les évolutions sans réécriture.

Concrètement :

- Un backend Python FastAPI qui expose un WebSocket bidirectionnel.
- Un client LLM abstrait derrière une interface `LLMClient`, dont la première implémentation tape LM Studio via l'API OpenAI-compatible. Changer de provider (Ollama, OpenAI cloud, Anthropic) = ajouter une classe.
- Un registry de composants UI côté backend (JSON Schema), injecté dans le system prompt du LLM. Le LLM répond en JSON strict `{speech: str, ui: [components]}`. Le backend valide, retry une fois en cas d'échec, fallback en texte brut sinon.
- Un front Tauri + React + Vite minimaliste : layout chat classique (historique scrollable + input bas), une registry locale qui mappe nom de composant → composant React, et un dispatcher qui rend la liste `ui[]` reçue.
- Deux composants V0 : `ChatMessage` (bulle conversationnelle) et `Markdown` (texte formaté).
- Historique conversation in-memory côté backend, indexé par session WebSocket. Une connexion = une conversation isolée.

L'utilisateur ouvre l'app, tape un message, voit la réponse de l'agent s'afficher comme une suite de composants pilotés par le LLM. Sous le capot, toute la plomberie qui permettra demain d'ajouter TTS streaming, function calling, persistance, et server-driven UI rich est déjà en place.

## User Stories

1. En tant qu'utilisateur, je veux ouvrir l'app desktop Bob et voir immédiatement une interface de chat fonctionnelle, pour pouvoir interagir avec mon LLM local sans config supplémentaire.
2. En tant qu'utilisateur, je veux taper un message dans un champ de saisie et l'envoyer avec Entrée, pour communiquer naturellement avec l'agent.
3. En tant qu'utilisateur, je veux voir mes messages apparaître instantanément dans la conversation, pour avoir un feedback visuel immédiat.
4. En tant qu'utilisateur, je veux voir la réponse de l'agent s'afficher sous forme de bulle distincte de mes propres messages, pour distinguer clairement qui parle.
5. En tant qu'utilisateur, je veux que les réponses de l'agent supportent le formatage Markdown (gras, italique, listes, code), pour lire confortablement des réponses structurées.
6. En tant qu'utilisateur, je veux que l'agent garde le contexte de notre conversation tant que la connexion est ouverte, pour pouvoir enchaîner des questions de suivi sans tout réexpliquer.
7. En tant qu'utilisateur, je veux pouvoir fermer et rouvrir l'app sans crash, même si LM Studio n'est pas démarré, pour ne pas avoir de friction de lancement.
8. En tant qu'utilisateur, je veux voir un indicateur clair quand le backend est déconnecté, pour comprendre pourquoi l'agent ne répond pas.
9. En tant qu'utilisateur, je veux que le front tente de se reconnecter automatiquement au backend si la connexion tombe, pour ne pas avoir à relancer manuellement.
10. En tant qu'utilisateur, je veux ouvrir deux instances de l'app en même temps avec deux conversations indépendantes, pour expérimenter en parallèle.
11. En tant qu'utilisateur, je veux que l'agent réponde dans un délai raisonnable (< 5s pour un message court avec Qwen 7B), pour que l'expérience reste fluide.
12. En tant qu'utilisateur, je veux voir un indicateur quand l'agent réfléchit (génération LLM en cours), pour savoir qu'il travaille.
13. En tant qu'utilisateur, je veux que si le LLM renvoie une réponse invalide, l'agent retente automatiquement puis dégrade vers du texte simple, pour ne jamais voir un message d'erreur cryptique.
14. En tant qu'utilisateur, je veux pouvoir scroller dans l'historique de la conversation actuelle, pour relire ce qui a été dit.
15. En tant qu'utilisateur, je veux que le scroll auto-suive le bas quand un nouveau message arrive, pour ne pas devoir scroller manuellement.
16. En tant que développeur, je veux changer le modèle LM Studio via une variable d'env, pour expérimenter Qwen 7B vs 14B sans rebuild.
17. En tant que développeur, je veux pouvoir pointer le backend vers Ollama ou un autre serveur OpenAI-compat via `LLM_BASE_URL`, pour switcher de provider sans modifier le code.
18. En tant que développeur, je veux ajouter un nouveau composant UI dispatchable (ex: `Card`) en touchant uniquement le registry backend (schéma) et le registry front (React component), sans modifier le protocole WS ni la logique de dispatch.
19. En tant que développeur, je veux inspecter les logs structurés du backend (`logs/llm-*.jsonl`) pour voir exactement ce qui a été envoyé au LLM et reçu en retour, afin de debugger les hallucinations ou JSON invalides.
20. En tant que développeur, je veux que les system prompts vivent dans des fichiers Markdown séparés, pour les itérer sans toucher au code Python.
21. En tant que développeur, je veux un repo monorepo avec `backend/` et `frontend/`, pour committer atomiquement les changements protocole WS qui touchent les deux côtés.
22. En tant que développeur, je veux que la stack soit installable en une commande par côté (`uv sync` backend, `pnpm install` front), pour onboarder rapidement un futur contributeur ou recréer l'env.
23. En tant que développeur, je veux que les modules de parsing/validation soient testables sans LM Studio (LLM client mocké), pour itérer sur la logique sans dépendre d'un binaire externe.
24. En tant que développeur, je veux que le backend bind uniquement sur `127.0.0.1`, pour qu'il ne soit jamais exposé accidentellement au réseau local.
25. En tant que développeur, je veux que le contrat WebSocket utilise un champ `type` discriminant sur chaque message, pour pouvoir ajouter de nouveaux types d'événements (`tool_call`, `ambient`, `speech_chunk`) plus tard sans casser l'existant.
26. En tant que développeur, je veux pouvoir hot-reloader le backend (`uvicorn --reload`) et le front (`vite dev`) pendant le dev, pour itérer rapidement.
27. En tant que développeur, je veux que les imports/typage soient vérifiés en CI locale (`ruff`, `mypy strict`, `tsc --noEmit`, `biome`), pour ne pas découvrir les erreurs en runtime.
28. En tant que développeur, je veux que le registry de composants côté backend soit la source unique de vérité du contrat UI, pour éviter la divergence entre ce que le LLM peut générer et ce que le front sait rendre.
29. En tant que développeur, je veux que toute réponse LLM passe par une étape de validation pydantic avant d'être envoyée au front, pour ne jamais propager un payload mal formé jusqu'à React.
30. En tant que développeur, je veux que l'historique conversation soit purgé proprement à la fermeture du WS, pour ne pas fuiter de mémoire entre sessions.

## Implementation Decisions

### Stack technique verrouillée

- **Backend** : Python 3.12+, FastAPI, uv (package manager), ruff (lint+format), mypy strict, structlog, pytest. Bind `127.0.0.1`.
- **Frontend** : Tauri 2 + React 18 + Vite, TypeScript strict, pnpm, Biome (lint+format), Tailwind CSS v4, Zustand.
- **LLM** : LM Studio en local (endpoint OpenAI-compatible `http://localhost:1234/v1`), modèle par défaut recommandé Qwen 2.5 7B Instruct. Client : SDK `openai` Python officiel.
- **Transport** : WebSocket unique entre front et backend, messages JSON typés discriminés par champ `type`.
- **Layout repo** : monorepo avec `backend/` et `frontend/` à la racine.
- **Config** : `.env` + `pydantic-settings`. Variables clés : `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `BACKEND_HOST`, `BACKEND_PORT`, `LOG_LEVEL`.
- **Prompts** : fichiers `.md` dans `backend/prompts/`, chargés au boot, templating simple via `str.format` ou jinja2.

### Modules backend

1. **`config`** — Charge `.env` via pydantic-settings. Expose un objet `Settings` immuable. Validation au démarrage : crash early si vars manquantes.
2. **`llm_client`** — Module deep, interface stable. Classe abstraite `LLMClient` avec une méthode `async chat(messages: list[Message], schema: dict | None) -> dict`. Implémentation `LMStudioClient` enveloppe `openai.AsyncOpenAI` avec `base_url` paramétré. Le swap vers Ollama ou OpenAI cloud = ajouter une classe sœur, aucun appelant ne change.
3. **`ui_registry`** — Source unique de vérité du contrat UI. Définit chaque composant disponible avec son nom et son JSON Schema de props. Expose `get_components_schema_for_prompt()` (description injectable dans le system prompt) et `get_response_schema()` (JSON Schema complet de la réponse LLM attendue, pour `response_format=json_schema` LM Studio + validation pydantic). Module deep et isolé, testable sans dépendances.
4. **`response_parser`** — Reçoit la string brute renvoyée par le LLM, tente parse JSON + validation contre `ui_registry.get_response_schema()`. Si échec, déclenche un retry unique via `llm_client` (avec message correctif ajouté à l'historique éphémère du retry). Si second échec, retourne `{speech: raw_text, ui: []}` en fallback. Module deep, testable isolément avec LLM client mocké.
5. **`prompts`** — Loader simple qui lit les `.md` de `backend/prompts/` au démarrage et expose un dict `name -> template`. Méthode `render(name, **vars)`.
6. **`conversation`** — Maintient un `dict[session_id, list[Message]]` in-memory. API : `append(session_id, message)`, `get_history(session_id)`, `clear(session_id)`. Cleanup au déconnect WS.
7. **`chat_service`** — Orchestrateur. Reçoit `(session_id, user_message)`. Construit la liste `messages` (system prompt + historique + user). Appelle `llm_client.chat(...)`. Passe la réponse à `response_parser`. Persiste user + assistant message dans `conversation`. Retourne la réponse validée typée.
8. **`ws_router`** — Définit `GET /ws/chat`. À la connexion : génère `session_id`, envoie `{type: "session", session_id}`. Boucle de lecture : parse messages entrants `{type: "user_msg", content}`, délègue à `chat_service`, renvoie `{type: "assistant_msg", speech, ui}`. Erreurs internes → `{type: "error", message}`. Disconnect → `conversation.clear(session_id)`.
9. **`logging_setup`** — Configure structlog : logs JSON sur stdout (niveau via `LOG_LEVEL`) + handler dédié pour LLM calls dans `logs/llm-{YYYY-MM-DD}.jsonl` (dump messages envoyés, réponse brute, latence, tokens si dispo).

### Modules frontend

- **`useWebSocket`** — Hook React custom. Wrappe `WebSocket` natif. Gère : reconnect exponentiel avec backoff, queue des messages envoyés pendant disconnect, parsing JSON typé à la réception, exposition `connectionStatus: 'connecting' | 'open' | 'closed'`.
- **`chatStore`** (Zustand) — État global : `messages: ChatMessage[]`, `connectionStatus`, `isWaitingResponse`, `sessionId`. Actions : `addUserMessage`, `addAssistantMessage`, `setStatus`, `setWaiting`.
- **`componentRegistry`** — Map TS `Record<string, React.ComponentType<any>>` qui mappe nom de composant backend → composant React. V0 : `ChatMessage`, `Markdown`. Ajouter un composant = ajouter une ligne au registry + le composant React.
- **`Dispatcher`** — Composant qui prend `ui: ComponentDescriptor[]` et rend la séquence en lookup dans `componentRegistry`. Composant inconnu → fallback visuel `<UnknownComponent name={...} />` (debug-friendly, ne crashe pas).
- **`ChatView`** — Layout : header simple, zone scrollable d'historique (auto-scroll bottom sur nouveau message), barre de saisie en bas (textarea + bouton Envoyer, Entrée envoie, Shift+Entrée newline). Lit `chatStore`, dispatche les `assistant_msg.ui[]` au `Dispatcher`.
- **`App`** — Root, monte `useWebSocket`, render `ChatView`.

### Contrat WebSocket V0

Messages client → serveur :
- `{type: "user_msg", content: string}`

Messages serveur → client :
- `{type: "session", session_id: string}` — envoyé à la connexion
- `{type: "assistant_msg", speech: string, ui: ComponentDescriptor[]}`
- `{type: "error", message: string, code?: string}`
- `{type: "thinking", state: "start" | "end"}` — indicateur de latence pendant l'appel LLM

`ComponentDescriptor` = `{component: string, props: Record<string, unknown>}`.

Pas de streaming token-par-token V0. Le backend attend la réponse JSON complète de LM Studio avant d'émettre `assistant_msg`. Le `thinking` event encadre l'attente côté UI.

### Contrat LLM (system prompt)

Le system prompt V0 contient :
- Description du rôle (assistant personnel, ton concis).
- L'instruction de toujours répondre en JSON conforme au schéma fourni.
- Le schéma `{speech: str, ui: [components]}` avec, pour chaque composant disponible, son nom et ses props attendues (généré dynamiquement par `ui_registry`).
- Une politique simple V0 : `speech` contient ce que l'agent dit. `ui` peut être vide, ou contenir un ou plusieurs composants à afficher en plus.

### Composants V0 (registry)

- **`ChatMessage`** — Props : `{role: "assistant" | "user", content: string}`. Rendu : bulle stylée selon rôle. V0 : le LLM peut ré-émettre des `ChatMessage` pour citer ou structurer, mais en pratique son `speech` est déjà rendu comme bulle assistante par défaut côté UI ; ce composant existe surtout pour valider le mécanisme.
- **`Markdown`** — Props : `{content: string}`. Rendu : Markdown parsé (lib `react-markdown` + GFM). Le LLM s'en sert pour insérer du texte riche dans `ui[]`.

### Gestion d'erreurs

- LM Studio injoignable → backend renvoie `{type: "error", message: "LLM provider unreachable", code: "LLM_UNREACHABLE"}`. Le front affiche un toast et propose retry manuel.
- JSON LLM invalide / schema violation → retry 1x avec message correctif → si échec, fallback `{speech: raw_text, ui: []}`. Loggué en WARN.
- Timeout LLM (configurable, défaut 60s) → `{type: "error", code: "LLM_TIMEOUT"}`.
- WS disconnect → front reconnect auto, conversation reset (in-memory backend = perdue, attendu V0).

## Testing Decisions

Décision : tests sur modules sélectionnés, côté backend uniquement V0. Front validé manuellement (pas encore de jeu d'interactions assez stables pour mériter Playwright/Vitest).

**Ce qui fait un bon test ici** : tester le comportement externe observable, pas les détails d'implémentation. Pour le backend, ça veut dire : entrée = stimulus typé (string LLM response, payload WS, etc.), sortie = objet validé typé ou exception attendue. Les LLM clients sont mockés (interface `LLMClient` abstraite → fake renvoyant des strings contrôlées) : on ne teste jamais LM Studio lui-même.

**Modules testés V0** :

1. **`response_parser`** — Cas : JSON valide conforme → `ParsedResponse` correcte ; JSON syntaxiquement invalide → trigger retry ; JSON valide mais schema violation (props manquantes, composant inconnu) → trigger retry ; échec retry → fallback `{speech: raw, ui: []}`. C'est le module le plus à risque, priorité tests.
2. **`ui_registry`** — Cas : `get_response_schema()` produit un JSON Schema valide (validable par `jsonschema.Draft202012Validator`) ; ajout d'un composant fictif au registry est reflété dans le schéma ; validation d'un payload conforme passe ; validation d'un payload non conforme échoue avec l'erreur attendue.
3. **`conversation`** — Cas : append + get_history retourne ordre correct ; sessions isolées (deux session_id ≠ ne se voient pas) ; clear vide la session ; opérations sur session inconnue ne crashent pas.
4. **`chat_service`** — Cas (avec `LLMClient` mocké) : un échange simple ajoute 2 messages à l'historique ; le system prompt est bien injecté en première position ; un retry parser n'écrit pas le message intermédiaire dans l'historique persistant ; fallback texte produit bien `{speech, ui: []}`.
5. **`ws_router`** (intégration légère via `TestClient` FastAPI) : la connexion émet d'abord `{type: "session"}` ; envoi d'un `user_msg` produit `thinking start` + `assistant_msg` + `thinking end` dans cet ordre ; disconnect purge la session.

**Hors V0** : pas de tests `llm_client` lui-même (ce serait tester le SDK openai), pas de tests `config` (pydantic-settings est déjà testé en amont), pas de tests `prompts` (loader trivial).

**Prior art** : pas de codebase existante à imiter. Suivre les conventions pytest standard : `tests/` à la racine de `backend/`, fixtures dans `conftest.py`, un fichier de test par module testé, noms `test_<module>_<scenario>.py` éventuellement regroupés.

## Out of Scope

- TTS (Piper, Kokoro, ElevenLabs) — V1+.
- STT (whisper.cpp, faster-whisper) — V1+.
- Streaming token-par-token de la réponse LLM — ajouté quand TTS arrive.
- Function calling / tool use — V1+.
- Persistance conversation (SQLite/Postgres) — V1+.
- RAG vectoriel — V2+.
- Knowledge graph (Neo4j/Kùzu) + extraction d'entités — V2+.
- Self-updating memory / agentic memory — V3+.
- UI Jarvis (Three.js, shaders, audio reactive viz, sphère centrale, ambient state) — V2+.
- Composants UI métier (`TaskList`, `Calendar`, `Note`, `Card`, etc.) — ajoutés au fil des features, hors V0.
- Auth backend (V0 = bind 127.0.0.1 only).
- Multi-utilisateur / partage de sessions.
- Build/release pipeline Tauri signé pour distribution.
- Tests UI/E2E front (Vitest, Playwright).
- Configuration multi-modèles dynamique runtime (V0 = un modèle par boot, env var).
- CI/CD (GitHub Actions, hooks pre-commit).
- Tests d'intégration end-to-end avec LM Studio réel.

## Further Notes

- L'architecture est volontairement conçue pour qu'aucune décision V0 ne ferme une porte future : le WebSocket bidirectionnel accueillera les chunks audio TTS/STT ; le champ `type` discriminé absorbera `tool_call`, `ambient`, `speech_chunk` ; le `ui_registry` permettra d'ajouter des composants Jarvis-rich sans toucher au protocole ; l'interface `LLMClient` permet de glisser vers Ollama, OpenAI cloud, ou Anthropic en V1.
- L'ontologie de composants V0 (`ChatMessage`, `Markdown`) est minimaliste pour valider la plomberie de bout en bout. Ajouter `Card`, `List`, `TaskList` se fera en suivant le même pattern : entrée registry backend (JSON Schema), entrée registry front (composant React), zéro changement protocole.
- Le pattern "retry 1x + fallback texte" est un compromis V0. Quand on aura un vrai jeu d'exemples d'échecs, on pourra ajuster (ex: prompt few-shot dans le retry, ou agent ReAct multi-tours).
- Logging brut des LLM calls (`logs/llm-*.jsonl`) sert deux usages : debug immédiat (voir ce qui a été envoyé/reçu), et futur jeu de données pour fine-tuning ou évaluation de prompt.
- Le `session_id` généré côté backend à la connexion est exposé au front pour qu'il puisse le logguer / l'afficher en debug, mais le front ne l'utilise pas dans son protocole — toute la corrélation se fait via la connexion WS elle-même.
- Choix Qwen 2.5 7B Instruct comme défaut : meilleur compromis FR + JSON structuré + vitesse sur Mac M-series 16Go. Si la machine est plus puissante, passer à Qwen 2.5 14B via `LLM_MODEL`.
- L'ordre de build recommandé pour exécuter ce PRD : (1) scaffold repo + tooling, (2) backend `config` + `llm_client` + ping LM Studio, (3) `ui_registry` + `response_parser` + tests, (4) `conversation` + `chat_service` + tests, (5) `ws_router` + `prompts` + test intégration, (6) front scaffold Tauri+Vite+Tailwind, (7) `useWebSocket` + `chatStore`, (8) `componentRegistry` + `Dispatcher` + composants V0, (9) `ChatView` + intégration, (10) validation manuelle end-to-end avec LM Studio + Qwen 7B.
