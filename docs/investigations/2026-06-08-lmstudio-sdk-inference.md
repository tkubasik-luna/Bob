# Investigation — Inférence LM Studio via le SDK `lmstudio` (au lieu de l'API OpenAI)

**Date :** 2026-06-08
**Statut :** investigation seule, aucun code écrit.
**Déclencheur :** utilisateur — « actuellement on utilise l'API OpenAI pour LM Studio,
je voudrais qu'on utilise le SDK LMStudio directement ».
**Décisions verrouillées (utilisateur, 2026-06-08) :** (1) migration **totale** — SDK
partout (`chat` + `stream_chat` + `complete` + `stream_complete`), pas seulement le
chat. (2) **préserver** le streaming incrémental des arguments de tool-call (chemin
`say`→TTS) via un override de l'API privée du SDK.

---

## TL;DR

- **Aujourd'hui :** l'inférence LM Studio passe par `openai.AsyncOpenAI` contre
  l'endpoint OpenAI-compatible `…/v1` (`LMStudioClient`, `backend/src/bob/llm_client.py`).
  Le SDK `lmstudio` n'est utilisé QUE pour le *management* (load/list/probe) dans
  `lm_studio_manager.py`. Les deux ne partagent aucun chemin de code (par design,
  documenté dans le module).
- **`chat` + `stream_chat` migrent proprement** sur l'API publique du SDK
  (`AsyncLLM.respond` / `respond_stream` + `response_format` pour le guided-JSON).
  Le streaming `reasoning` devient **plus propre** que le hack actuel
  `delta.reasoning_content` : le SDK marque chaque fragment via `reasoning_type`.
- **Le tool-calling est le point dur.** Le SDK n'a **aucun** chemin « rends-moi les
  tool-calls » en un coup : `respond()`/`PredictionResult` n'exposent jamais les
  tool-calls. Seul `act()` le fait — mais `act()` est un **exécuteur agentique** qui
  appelle lui-même tes callables Python en boucle multi-rounds. Ça entre en collision
  frontale avec le modèle de Bob (l'**orchestrateur** dispatche les tool-calls en
  sous-tâches). Solution : descendre au primitif privé `ChatResponseEndpoint` +
  `AsyncPredictionStream._iter_events()` et capturer `PredictionToolCallEvent.arg`
  (`ToolCallRequest{name,id,arguments}`) **sans exécuter**.
- **Régression voix à éviter :** le wire LM Studio émet des fragments d'arguments
  incrémentaux (`toolCallGenerationArgumentFragmentGenerated`) mais **le SDK 1.5.0 les
  jette** (`pass  # UI event, currently ignored by Python SDK`,
  `PredictionEndpoint.iter_message_events`). Le `say` tool dépend de ces fragments pour
  démarrer le TTS sur les premiers mots (PRD 0016). Décision : on **subclasse**
  `iter_message_events` pour ressurface ces events → test de garde obligatoire contre
  les upgrades du SDK.
- **Dommages collatéraux (dans le périmètre) :** le **codec natif** est court-circuité
  pour LM Studio (le codec Hermes reste pour Claude CLI) ; les `ToolDefinition` se
  convertissent en `ToolFunctionDef` SDK ; self-correction + fixtures golden + suites
  de tests à reprendre ; modèle de config (`LLM_BASE_URL` `/v1` → host:port SDK).

---

## État actuel (vérifié)

### Transport
- `backend/src/bob/llm_client.py:418-421` — `AsyncOpenAI(base_url=LLM_BASE_URL,
  api_key=LLM_API_KEY)` instancié dans `LMStudioClient.__init__`.
- 4 call-sites `self._client.chat.completions.create(...)` :
  - `chat` (ligne 517, non-stream), `stream_chat` (691, stream),
    `complete` (853, tools non-stream), `stream_complete` (1078, tools stream).
- Modèle par requête : champ wire `model` = override par-rôle (`model` ctor arg) sinon
  `settings.LLM_MODEL` (`_model` property, 433-442).
- `reasoning` par-rôle : `extra_body={"reasoning": level}` (444-456) — champ non-OpenAI
  forwardé via `extra_body`.
- `response_format={"type":"json_schema","json_schema":schema}` pour le guided-JSON
  (489-493, 661-665).
- `stream_options={"include_usage": True}` pour récupérer l'usage en stream (655-659).
- `max_tokens=4096` en dur partout.
- Fold rôle `system_validator`→`system` + assert rôles standard avant chaque appel
  (issue 0048 ; `_normalise_validator_role`, `_assert_standard_roles`).

### Tool-calling (couche codec)
- `backend/src/bob/llm/tooling/` — codec canonique (native / Hermes / guided), `ToolSpec`,
  `select_codec`, `capability_for_backend`. `LMStudioClient` choisit le codec **natif**
  (426-430). `complete`/`stream_complete` : `codec.inject()` (bloc OpenAI `tools`+
  `tool_choice`) puis `codec.parse()` / `codec.stream_parser()`.
- Le `stream_parser` ré-émet les `tool_call_*` chunks ; le `say` tool branche
  `args_delta` → `PartialJsonParser` → `speech_delta` → TTS (PRD 0006/0011, 0016).

### Management (déjà SDK — modèle de référence)
- `backend/src/bob/lm_studio_manager.py` — seule frontière sur `lmstudio`. `Client(host)`
  synchrone, `host_from_base_url()` dérive `host:port` depuis `LLM_BASE_URL` (enlève
  `//` et `/v1`). SDK faké en test → suite offline.

---

## Capacités SDK `lmstudio` 1.5.0 (vérifié par introspection)

| Besoin Bob | Chemin SDK | Verdict |
|---|---|---|
| chat non-stream + guided-JSON | `AsyncLLM.respond(history, response_format=schema, config=...)` → `PredictionResult.content`/`.parsed` | ✅ public |
| chat stream + reasoning | `AsyncLLM.respond_stream(...)` → `LlmPredictionFragment{content, reasoning_type, tokens_count}` | ✅ public, reasoning natif |
| perf (ttft, tok/s, tokens) | `PredictionResult.stats` / `LlmPredictionStats{time_to_first_token_sec, tokens_per_second, prompt_tokens_count, predicted_tokens_count, total_tokens_count}` | ✅ |
| tool-calls « un coup » | ❌ aucun — `respond()` n'expose pas les tool-calls | ⚠️ → low-level |
| tool-calls (exécution déléguée) | `AsyncLLM.act(...)` exécuteur agentique multi-rounds | ❌ mauvais fit |
| tool-calls capturés sans exécuter | `ChatResponseEndpoint(..., llm_tools=parse_tools(...))` + `AsyncPredictionStream._iter_events()` → `PredictionToolCallEvent.arg : ToolCallRequest{type,name,id,arguments}` | ⚠️ **API privée** |
| args de tool-call incrémentaux | wire `toolCallGenerationArgumentFragmentGenerated` **jeté** par le SDK (`iter_message_events`) ; seul `toolCallGenerationEnd` (call complet) ressort | ⚠️⚠️ override privé |
| handle modèle | `AsyncClient.llm.model(key)` → `AsyncLLM` | ✅ |
| config | `LlmPredictionConfig{max_tokens, temperature, structured, raw_tools, reasoning_parsing, raw, ...}` | ✅ (mapping `reasoning` → à valider) |

Transport SDK = **websocket** LM Studio sur `host:port` (pas le SSE OpenAI `/v1`).
`AsyncClient` est un contexte async (gérer `connect`/`aclose`).

---

## Architecture cible

### Mapping par méthode
1. **`chat(messages, schema)`** → convertir `messages` (après fold validator) en `Chat`
   SDK → `model.respond(chat, response_format=schema, config={max_tokens:4096, ...})`.
   Lire `.content` (fallback `.parsed`), tokens via `.stats`. Erreurs `LMStudioError*` →
   `LLMClientError`. Debug events + `log_llm_call` conservés.
2. **`stream_chat(messages, schema)`** → `model.respond_stream(...)` ; itérer fragments :
   `reasoning_type != none` → chunk `reasoning` ; sinon `content` → chunk `text`. Stats
   finales → `perf` chunk. Reconstruit byte-pour-byte la string de `chat` (parse action
   sous-agent inchangé).
3. **`complete(messages, tools)`** → construire `ChatResponseEndpoint` avec
   `llm_tools = ChatResponseEndpoint.parse_tools(<ToolFunctionDef[]>)`, dérouler
   `_iter_events()`, **collecter** les `PredictionToolCallEvent.arg` (NE PAS exécuter),
   mapper `ToolCallRequest` → `bob.llm.types.ToolCall`. Pas de tool-call → texte
   (`PredictionResult.content`).
4. **`stream_complete(messages, tools)`** → même endpoint low-level ; fragments texte →
   chunks `text`/`reasoning` ; tool-calls → cycle `tool_call_start` →
   `tool_call_args_delta` (incrémental, voir override) → `tool_call_end`.

### Override de streaming des args (décision verrouillée)
- Subclasser `ChatResponseEndpoint` (ou son parent `PredictionEndpoint`) pour overrider
  `iter_message_events` : sur `toolCallGenerationArgumentFragmentGenerated`, émettre un
  nouvel event portant le fragment d'arguments → mappé en `tool_call_args_delta`. Garde
  le `say`→TTS early-start identique.
- **Test de garde** : un test qui échoue si l'upgrade du SDK change la forme de l'event
  ou réintègre/renomme le handler — sentinelle contre la fragilité API privée.

### Conversion des outils
- `ToolDefinition` Bob → `ToolFunctionDef`/`ToolFunctionDefDict` SDK (name, description,
  parameters=JSON Schema). Pas de callable réel (capture seule) — passer une
  implémentation sentinelle jamais appelée (on s'arrête à la capture, `act` non utilisé).
- Le **codec natif** n'est plus la frontière LM Studio pour les tools. Garder
  `ToolSpec.order_specs` (ordre déterministe) en amont de la conversion. Le codec Hermes
  reste pour `ClaudeCliClient`.

### Config & lifecycle
- Inférence repointée du `/v1` vers `host:port` SDK (réutiliser `host_from_base_url`).
  Décider : garder `LLM_BASE_URL` comme source (dériver host) ou ajouter un réglage
  dédié. Per-rôle : un `AsyncClient` par rôle pinné sur son host + `client.llm.model()`.
- Mapping `reasoning` (off/low/medium/high/on) : **OUVERT** — `LlmPredictionConfig` n'a
  pas de champ « effort » ; candidats `config.raw` (passthrough) ou un champ spécifique.
  À confirmer contre un LM Studio réel.
- Lifecycle `AsyncClient` : un client long-vécu par rôle, fermé au swap (intégration
  `llm_swap.py` `RoleLLMSwitcher` / `LLMSwitcher`). Le management garde son client
  éphémère par appel.

---

## Risques / robustesse (barre #1)

- **API privée du SDK** (`_iter_events`, `iter_message_events`, `ChatResponseEndpoint`) :
  casse silencieuse à l'upgrade. Mitigation : surface d'accès minimale + isolée dans un
  seul module + test de garde + pin de version `lmstudio`.
- **Régression latence voix** si le streaming d'args échoue → le `say`→TTS retombe en
  whole-call. Test : vérifier que `tool_call_args_delta` arrive AVANT `tool_call_end`.
- **Parité tests** : `test_llm_client.py` et les fixtures golden (0057) supposent la
  forme wire OpenAI. À réécrire pour le fake SDK (modèle : `test_lm_studio_manager.py`
  qui fake déjà la frontière SDK).
- **Double transport transitoire** : Claude CLI inchangé ; seul `LMStudioClient` bascule.
  Garder `FakeLLMClient` pour les tests d'orchestrateur.
- **Usage/tokens** : vérifier la présence des stats en stream (le SDK ne dépend pas du
  `include_usage` OpenAI — stats natives via `PredictionResult.stats`).

---

## Décisions (grill 2026-06-08)

| # | Décision | Détail |
|---|---|---|
| Strat | **Side-by-side + flag, puis purge** | Nouvelle classe `LMStudioSDKClient`, sélectionnée par `LLM_LMSTUDIO_TRANSPORT=sdk\|openai`. Validation bout-en-bout sur LM Studio réel, puis suppression du transport OpenAI + dép `openai`. Rollback instantané au bring-up. |
| Q2 | **`LLM_BASE_URL` source unique** | `host_from_base_url()` dérive le `host:port` (comme le management). Per-rôle override marche tel quel. Pas de réglage host dédié. |
| Q4 | **Client long-vécu par rôle** | Un `AsyncClient` persistant par rôle (lazy-connect, ws amorti → TTFT bas voix), fermé/recréé par `RoleLLMSwitcher` au swap. Reconnect-on-drop (`LMStudioWebsocketError` → reconnect + retry une fois). |
| Q3 | **Subclass `ChatResponseEndpoint` + override `iter_message_events`** | `BobChatResponseEndpoint` ressurface `toolCallGenerationArgumentFragmentGenerated` en event custom → `tool_call_args_delta`. Contenu, testable. Test de garde sentinelle obligatoire. |
| Q1 | **`config.raw` passthrough `{"reasoning": level}`** | Équivalent SDK de l'`extra_body` actuel. Parité validée au POC `chat()` ; fallback omission si aucun effet. Préserve le contrôle per-rôle (0108). |
| Q5 | **Outils SDK-natifs = seul chemin LM Studio** | Advertisement via `ToolFunctionDef`/`raw_tools` (pas de codec). `LLM_TOOL_MODE` gardé pour le transport OpenAI (side-by-side) + Claude CLI (hermes) ; `guided`/`hermes` rejetés pour LM Studio SDK. `supports_guided_json` reste `True` (`chat(schema)`→`response_format`). |
| Q6 | **`count_tokens`/`get_context_length` hors scope** | Garder `_estimate_tokens` (summary début) + vrais counts via `PredictionResult.stats`. Budget context = enhancement séparé. |
| Tests | **Fake SDK offline + test de garde contractuel** | Fake `AsyncClient`/`AsyncLLM` + itérateur d'events scripté (modèle `test_lm_studio_manager`). Suite offline/déterministe. Garde = contrat sur dicts de messages canal fakés. LM Studio réel = POC manuel, hors CI. |

### Points mécaniques vérifiés (pas de décision)
- **Structured output** : `LlmStructuredPredictionSetting{type, json_schema, gbnf_grammar}` ≈ forme Bob → remap `{"type":"json","jsonSchema": <schema>}`, gating réel. ✅
- **Historique→Chat** : `Chat.add_system_prompt/add_user_message/add_assistant_response/add_tool_result(s)/from_history` → round-trip tool-calls/résultats supporté (converter à écrire). ✅
- **Connexion** : `AsyncClient` = ws persistant (lazy-connect, `aclose`) ; chaque prédiction = channel léger → long-vécu amorti. ✅
- **Erreurs** : `LMStudioError*` → `LLMClientError` (mapping conservé). Garde empty-content/no-result conservée (`PredictionResult.content` vide → raise).

---

## Prochaines étapes proposées

1. (optionnel) **grill** des 6 questions ouvertes ci-dessus.
2. **to-prd** → `prd/0017-lmstudio-sdk-inference.md` (modules : transport SDK, mapping
   chat/stream, capture tool-calls low-level, override streaming args + test de garde,
   conversion outils, config/lifecycle, reprise tests).
3. **to-issues** (tracer-bullet : `chat()` d'abord, bout-en-bout sur LM Studio réel).
4. **implement-feature** (AFK, max parallélisme).
