## Parent

prd/0017-lmstudio-sdk-inference.md

## What to build

Ajouter `LMStudioSDKClient.complete(messages, tools, session_id)` : faire émettre des
tool-calls au modèle via le format natif du SDK, et les **capturer sans les exécuter**
(modèle de dispatch orchestrateur de Bob), plus le converter d'outils (M4) et l'extension
historique pour les tours d'outils (M3).

End-to-end :
- Converter d'outils (M4, deep/pur) : `ToolDefinition[]` Bob → définitions d'outils natives
  du SDK (paramètres = JSON Schema), ordre déterministe conservé.
- Construire l'endpoint chat low-level du SDK avec ces outils, dérouler les events de
  prédiction, **collecter** les `ToolCallRequest` (sans appeler de callable / sans boucle
  `act()`), mappés en `ToolCall` Bob (id, nom, arguments décodés).
- Arguments malformés → `LLMClientError` (parité avec les fixtures golden existantes).
- Pas de tool-call → réponse texte (`LLMResponse(text=…)`), garde contenu vide conservée.
- Extension du converter historique (M3) : représenter les tours antérieurs
  assistant-avec-tool_calls + résultats d'outils dans le `Chat` SDK.
- Le codec natif OpenAI n'est plus utilisé pour LM Studio ; `LLM_TOOL_MODE` `guided`/
  `hermes` → rejet clair pour LM Studio SDK ; `supports_guided_json` reste `True`.

Démontrable : en mode SDK, l'orchestrateur reçoit un `LLMResponse` avec tool-calls et
dispatche les sous-tâches comme aujourd'hui (non-streaming).

## Acceptance criteria

- [ ] `complete(tools=…)` en mode SDK retourne un `LLMResponse` avec les tool-calls
      capturés **sans** que le SDK exécute aucun outil.
- [ ] Converter d'outils (M4) isolé et testé : `ToolDefinition` → définition SDK
      (paramètres JSON Schema) ; `ToolCallRequest` → `ToolCall` (ids/noms/arguments).
- [ ] Arguments de tool-call malformés → `LLMClientError` (parité golden).
- [ ] Pas de tool-call → texte ; contenu vide → `LLMClientError`.
- [ ] Le converter historique (M3) round-trippe les tours assistant+tool_calls + résultats
      d'outils en `Chat` SDK (testé).
- [ ] `LLM_TOOL_MODE=guided|hermes` rejeté proprement pour LM Studio SDK ; `auto`/`native`
      → outils SDK-natifs ; `supports_guided_json()` reste `True`.
- [ ] Tests offline avec SDK faké (events scriptés incluant un `ToolCallRequest`).

## Blocked by

- issues/0111-lmstudio-sdk-chat-poc-flag.md
