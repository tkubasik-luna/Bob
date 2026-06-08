## Parent

prd/0017-lmstudio-sdk-inference.md

## What to build

Tranche critique (latence voix). Ajouter `LMStudioSDKClient.stream_complete(messages,
tools, session_id)` avec **streaming incrémental des arguments de tool-call**, ce qui
exige l'override de l'API privée du SDK (M6) pour ressurfacer les fragments d'arguments
que le SDK 1.5.0 jette.

End-to-end :
- Sous-classe de l'endpoint de réponse chat du SDK qui override sa méthode d'itération
  des events de message pour émettre un event interne sur chaque
  `toolCallGenerationArgumentFragmentGenerated` (au lieu de le jeter).
- `stream_complete()` déroule cet endpoint et produit la séquence `StreamChunk` :
  fragments texte/reasoning → chunks ; tool-call → `tool_call_start` →
  `tool_call_args_delta` (incrémental, alimente le `PartialJsonParser` → `speech_delta`
  du `say` tool) → `tool_call_end` (parse final ; malformé → `LLMClientError`).
- Stats finales → chunk `perf`. Réutilise l'adapter de streaming (M5) + le converter
  d'outils (M4) + la capture sans exécution (issue 0113).
- Test de garde contractuel sur l'override + pin de version `lmstudio`.

Démontrable : en mode SDK, le `say` tool démarre le TTS dès les premiers mots, identique
au transport OpenAI (PRD 0016).

## Acceptance criteria

- [ ] La sous-classe d'endpoint (M6) ressurface les fragments d'arguments incrémentaux ;
      isolée dans un module unique.
- [ ] `stream_complete(tools=…)` en mode SDK yield `tool_call_start` →
      `tool_call_args_delta` (≥1, incrémental) → `tool_call_end`, sans exécuter d'outil.
- [ ] **Critère dur** : au moins un `tool_call_args_delta` est émis **avant**
      `tool_call_end` (démarrage TTS précoce préservé).
- [ ] Arguments finaux malformés → `LLMClientError` (parité avec `complete()`).
- [ ] Mode texte (pas de tool-call) → chunks `text` ; chunk `perf` final avec tokens/TTFT/
      tok/s.
- [ ] **Test de garde contractuel** : alimente la sous-classe avec des dicts de messages
      canal fakés (dont le fragment d'arguments) et asserte l'event attendu ; échoue
      bruyamment si la forme wire / le handler du SDK change.
- [ ] Version `lmstudio` pinnée.
- [ ] Tests offline (itérateur d'events scripté avec arg-fragments + tool-call-end).

## Blocked by

- issues/0112-lmstudio-sdk-stream-chat.md
- issues/0113-lmstudio-sdk-complete-tool-capture.md
