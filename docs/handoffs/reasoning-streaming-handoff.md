# Handoff — Reasoning streaming word-by-word (LM Studio)

> Pour une nouvelle conversation Claude Code. Contexte autonome — tout est ici.

---

## ✅ SPIKE RÉSOLU (2026-05-31) — résultats vérifiés en live

Spike script: `backend/spike_reasoning.py` (jetable). Endpoint live, modèle
reasoning `google/gemma-4-e4b` (6.3GB, seul reasoning-model qui tient sur la box ;
`qwen3.6-35b` + `magistral-small` OOM). Résultats mesurés :

| Test | reasoning | answer | progress | perf stats | schema |
|------|-----------|--------|----------|-----------|--------|
| `/v1` **sans** schema (Jarvis) | ✅ 577 ch | ✅ | ❌ | via `include_usage` | n/a |
| `/v1` **avec** `json_schema` (sub-agents) | ❌ **0** (supprimé) | ✅ 19 ch | ❌ | ✅ | ✅ |
| native `/api/v1/chat` `reasoning:on` | ✅ delta×232 | ✅ delta×185 | ✅ `prompt_processing.progress`×3 | ✅ `chat.end.stats` | ❌ |

**Hypothèse (b) CONFIRMÉE** : `response_format: json_schema` **supprime** le
`reasoning_content` sur `/v1` (0 ch avec schema vs 577 sans). C'est la vraie cause
du mode dégradé des sub-agents — PAS l'endpoint.

**Native rejette** `response_format` ET `structured` (`unrecognized_keys`) → pas de
sortie contrainte par schéma sur l'API native. Confirme le conflit invariant.

**Jarvis tool-calling** = fonction-calling OpenAi inline sur `/v1`
(`stream_complete(tools=…)` → deltas `tool_call_*`). Native ne prend QUE des
`integrations` MCP, pas de tool defs inline → **native casserait les tools de Jarvis**.

### Décision d'architecture (robustesse d'abord) — TOUT reste sur `/v1`

- **Jarvis** : reasoning_content **déjà streamé** sur `/v1` ET **déjà câblé** au feed
  (`orchestrator.py:808-821` → `_emit_jarvis_reasoning` → events `reasoning_delta`
  `agent_ref="jarvis"`). Backend reasoning Jarvis = **FAIT**. Ne paraissait mort que
  parce qu'aucun reasoning-model n'était chargé. `.env` `LLM_MODEL` pointé sur
  `google/gemma-4-e4b`.
- **Sub-agents** : restent `/v1` + `json_schema` → validation guided-JSON préservée
  (invariant intact). Reasoning supprimé par le schéma = inhérent → fallback steps
  narrés (déjà livré, issue 0070).
- **Pas de client natif.** Casserait tools Jarvis + invariant sub-agents, sur une box
  qui ne fait tourner que des ~4B (self-correction native trop fragile).
- **Seule perte** : les **barres de progression %** (`model_load` / `prompt_processing`)
  sont native-only → non alimentables sans sacrifier tools/invariant. UI = spinners
  indéterminés sur les phases d'attente. Tout le reste de la maquette est alimentable.

### Plan de build (maquettes dans `Design Mockup/agents.jsx` + CSS dans le `.html`)

État :
1. ✅ **Backend perf** — `stream_options:{include_usage:true}` sur les 2 chemins stream ;
   helpers `_read_usage` / `_build_perf_chunk` (`llm_client.py`) ; nouveau `StreamChunk`
   kind `perf` (`llm/types.py`) ; `ReasoningStreamReader.perf` ; `agent_perf_frame`
   (`activity_projector.py`) ; émis par runner (`_emit_perf`) + orchestrator
   (`_emit_jarvis_perf`) → event WS `agent_perf`. **Vérifié live** (tokens/ttft/tok_s).
2. ✅ **Backend chips args/result** (B2) — `AgentActivity` + `to_wire` portent `args`/`result` ;
   `ToolCallStarted/Finished` portent args bruts + result structuré ; projecteur les
   redige (Mail-scrub via `redact_payload`) + résume sans contenu (`_summarise_args` →
   "k: v · …", `_summarise_result` → "N messages"). Runner passe `action.args` +
   `result.result`. Frontend : `AgentActivityMsg.args?/result?` → `ChipItem` → rendu
   `al-chip-args`/`al-chip-result` + CSS. ⚠️ chaque emit atterrit dans le ring-buffer
   debug (même `debug_event=None`) → la redaction Mail est obligatoire (testée).
3. ✅ **Frontend store** — slice `perfByAgent` + action `setPerf` (+ clear/reset) ;
   type `AgentPerfMsg` (`types/ws.ts`) ; case WS bridge (`useChatWsBridge.ts`).
   Modèle timeline (reasoning+chips) conservé (non destructif).
4. ✅ **Frontend UI** — `AgentBlock` réécrit = lane maquette (PhaseRow + thinking box
   collapsible + tool chips + perf footer + error inline + summary 0074). CSS `al-*`
   porté dans `styles/hud.css` (tokens déjà présents). Shell `agent-panel` réutilisé.
5. ✅ **Phases client-side** — `lib/agentPhase.ts` (`deriveAgentPhase`, testé) :
   waiting → thinking → tool → done/error. Spinner indéterminé (pas de %).

Tests : backend 333 pass, frontend 252 pass, ruff/mypy/tsc/biome clean. `.env`
`LLM_MODEL=google/gemma-4-e4b` (chargé via `lms load`). Spike supprimé (résultats ci-dessus).

Modèles reasoning sur la box : `google/gemma-4-e4b` (7.5B, **tient**), `magistral-small`
(24B, OOM), `qwen3.6-35b` (OOM). `reasoning` param natif : ce modèle accepte `off`/`on`
(pas `high`).

---

## Objectif

Faire en sorte que le **feed d'activité agents** (PRD 0011) affiche la réflexion
des agents **mot-par-mot en live** quand on tourne sur **LM Studio + un modèle
reasoning**. Aujourd'hui : pas de stream token-par-token, le feed retombe sur les
"steps narrés" (le thought s'affiche d'un bloc).

## État actuel du code (déjà livré)

- Branche : `feat/0011-agent-activity-feed` (PRD 0011 entièrement implémenté +
  mergeable). Feature doc : `docs/features/0011-agent-activity-feed.md`.
- Le pipeline reasoning est en place et **fonctionne côté plomberie** :
  - `StreamChunk` a un kind `reasoning` (`reasoning_delta`) — `backend/src/bob/llm/types.py`.
  - `LMStudioClient.stream_chat` / `_consume_chat_stream` lisent
    `delta.reasoning_content` et émettent des chunks `reasoning` —
    `backend/src/bob/llm_client.py:649` (sub-agent) et `:1043` (stream_complete / Jarvis).
  - `ReasoningStreamReader` sépare canal reasoning vs content, expose `degraded`
    (= aucun reasoning vu) — `backend/src/bob/sub_agent/reasoning_stream.py`.
  - `SubAgentRunner` stream via ce reader ; **l'action est TOUJOURS validée
    depuis le content final agrégé** (guided-JSON intact, invariant verrouillé) —
    `backend/src/bob/sub_agent/runner.py` (`_stream_iteration`).
  - Fallback steps narrés quand `degraded` (issue 0070).
  - Frontend : `activityFeedStore` + `AgentBlock` + `AgentLanes` +
    `AgentActivityPanel` (overlay flottant droit). OK.

## Le problème (diagnostic fait)

Sur le run de l'utilisateur (LM Studio), **aucun chunk reasoning n'est émis** →
mode dégradé → pas de mot-par-mot.

### Config réelle
- `LLM_MODEL=qwen/qwen3.5-9b` → **modèle reasoning-capable** (Qwen3.5 produit du
  thinking). Donc le modèle n'est PAS le problème.
- `LLM_BASE_URL=http://192.168.86.21:1234/v1` → endpoint **OpenAI-compatible**
  (`/v1/chat/completions`), via `openai.AsyncOpenAI`. **PAS** l'API native LM
  Studio (`/api/v1/chat`).

### Preuves (logs `backend/logs/llm-2026-05-31.jsonl`, 184 appels)
- ⚠️ Le log par appel (`log_llm_call`, `backend/src/bob/logging_setup.py`)
  n'enregistre QUE `text_buffer` (le content), **jamais** le reasoning
  (volontaire, cf. `llm_client.py:628`). Donc le jsonl ne peut pas confirmer
  directement la présence de reasoning.
- **0 balise `<think>`** dans les `raw_response` → le raisonnement n'est pas
  embarqué dans le content.
- Appel sub-agent final `"Aucun email n'a été trouvé…"` : `tokens_out=21` pour
  ~21 tokens de content → **aucun écart** → aucun token de reasoning généré.
- Les appels sub-agent sont du guided-JSON propre (`{"action":"tool_call",…}`),
  `tokens_out=None` (LM Studio ne renvoie pas `usage` sans `stream_options:
  {include_usage:true}`).

### Deux hypothèses (à départager)
- **(a) Endpoint** — sur `/v1` (OpenAI-compat), LM Studio ne propage pas
  `reasoning_content` de façon fiable. Le reasoning fiable vit sur l'**API
  native** `/api/v1/chat` via les events `reasoning.delta`. → cf. doc ci-dessous.
- **(b) Guided-JSON** — `response_format: {type:"json_schema"}` (utilisé par les
  sub-agents via `chat`/`stream_chat(schema=…)`) **supprime** le `reasoning_content`
  en LM Studio (le modèle va droit à la sortie structurée). Cohérent avec les
  tokens sans écart.

Les deux peuvent être vraies en même temps.

## La doc fournie

`docs/reference/lmstudio-chat-streaming-events.md` — décrit l'**API native LM
Studio** `POST /api/v1/chat` avec `stream: true` (SSE, events nommés). Points
clés :
- Events `reasoning.start` / `reasoning.delta` (`content`) / `reasoning.end` —
  **canal reasoning premier-niveau, fiable**.
- `message.start` / `message.delta` (`content`) / `message.end` — content
  mot-par-mot.
- `tool_call.start` / `tool_call.arguments` / `tool_call.success|failure`.
- `chat.end` → `result.output` (liste typée: reasoning|tool_call|message) +
  `result.stats.reasoning_output_tokens` (ex. `5` → le reasoning peut être
  minuscule sur un tour avec tool_call).
- ⚠️ La doc ne montre **aucun** paramètre `response_format` / `schema` sur
  `/api/v1/chat`. **À vérifier** : supporte-t-elle la sortie contrainte par
  schéma ? C'est le risque n°1 (voir invariant).

## Invariant à NE PAS casser

L'action des sub-agents doit rester **parsée/validée depuis le content final
agrégé** (guided-JSON). Le reasoning est purement cosmétique. Si on passe à
l'API native et qu'elle ne supporte pas le schema, on perd la validation
contrainte → on retombe sur la **self-correction loop** existante (elle gère le
JSON malformé, mais c'est plus fragile sur petits modèles). Décision produit à
prendre avant de coder.

## Prochaines étapes recommandées (dans l'ordre)

1. **SPIKE bon marché d'abord** — départager (a) vs (b) sans gros chantier :
   - Test 1 : appel `/v1/chat/completions` à `192.168.86.21:1234` avec
     `qwen/qwen3.5-9b`, `stream:true`, **sans** `response_format` → inspecter si
     `delta.reasoning_content` (ou `<think>` dans `delta.content`) apparaît.
   - Test 2 : même appel **avec** `response_format: json_schema` → comparer.
   - Si reasoning apparaît sans schema mais pas avec → hypothèse (b) confirmée.
   - Si reasoning n'apparaît jamais sur `/v1` → hypothèse (a), il faut l'API native.
   - (Outil simple : un script Python `httpx`/`openai` ponctuel, ou `curl` SSE.)
2. **Si (a) — implémenter un client SSE natif** (`/api/v1/chat`) :
   - Nouveau client (httpx + parse SSE ligne `event:` / `data:`) qui mappe
     `reasoning.delta`→ chunk `reasoning`, `message.delta`→ chunk `text`,
     `tool_call.*`→ chunks tool-call, `chat.end`→ agrégat final.
   - Le brancher derrière la même interface `stream_chat` / `stream_complete`
     (garder `StreamChunk` comme contrat) pour ne RIEN changer en aval
     (`ReasoningStreamReader`, runner, feed marchent tels quels).
   - Vérifier le support schema ; sinon valider l'action via la self-correction.
   - Sélection par capability/flag : native pour LM Studio reasoning, OpenAI-compat
     sinon. Voir `bob.llm.tooling` (`capability_for_backend`, `select_codec`).
3. **Si (b) — décider** : accepter pas-de-reasoning sous guided-JSON (garder la
   validation), OU émettre un premier appel reasoning libre puis un second appel
   structuré (coût x2, cf. option écartée au design), OU passer en native.
4. **Observabilité** (utile quoi qu'il arrive) : logger le **nombre de chunks
   reasoning** par appel (et/ou `reasoning_output_tokens`) dans `log_llm_call`
   pour confirmer en prod. Aujourd'hui invisible.

## Fichiers clés

| Rôle | Fichier |
|------|---------|
| StreamChunk (contrat) | `backend/src/bob/llm/types.py` |
| Lecture reasoning_content (OpenAI-compat) | `backend/src/bob/llm_client.py` (`_consume_chat_stream` ~600-680 ; `stream_complete` ~882-1050) |
| Reader reasoning/content + degraded | `backend/src/bob/sub_agent/reasoning_stream.py` |
| Boucle sub-agent streamée | `backend/src/bob/sub_agent/runner.py` (`_stream_iteration`) |
| Émission Jarvis | `backend/src/bob/orchestrator.py` (`_stream_jarvis_call`, `_emit_jarvis_*`) |
| Capabilities / codec | `backend/src/bob/llm/tooling.py` |
| Log par appel | `backend/src/bob/logging_setup.py` (`log_llm_call`) |
| Doc API native | `docs/reference/lmstudio-chat-streaming-events.md` |
| Frontend feed | `frontend/src/store/activityFeedStore.ts`, `components/AgentBlock.tsx`, `AgentActivityPanel.tsx` |

## Config / accès

- Endpoint LM Studio : `http://192.168.86.21:1234` (OpenAI-compat sur `/v1`,
  native sur `/api/v1`). Modèle : `qwen/qwen3.5-9b`. Défini dans `.env` racine.
- Checks : backend `cd backend && uv run ruff check . && uv run mypy . && uv run pytest` ;
  frontend `cd frontend && pnpm biome check . && pnpm tsc --noEmit && pnpm test`.
- Note : `test_config.py::test_settings_loads_from_env` échoue déjà (env-leak
  `LLM_TIMEOUT_SECONDS`), non lié. + 4 erreurs mypy pré-existantes dans des tests
  non touchés. À ignorer.
