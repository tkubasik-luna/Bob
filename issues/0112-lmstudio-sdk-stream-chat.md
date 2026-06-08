## Parent

prd/0017-lmstudio-sdk-inference.md

## What to build

Ajouter `LMStudioSDKClient.stream_chat(messages, schema, session_id)` via le streaming
natif du SDK, et le cœur de l'adapter de streaming (module M5) pour le cas sans outils.

End-to-end :
- Appelle `model.respond_stream(...)` (mêmes conversions historique + `response_format` +
  `config.raw` reasoning que `chat()`).
- Adapter d'events SDK → `StreamChunk` Bob : fragments marqués reasoning → chunks
  `reasoning` ; fragments de contenu → chunks `text` ; stats finales → chunk `perf`
  (TTFT, tok/s, tokens).
- Invariant : la concaténation des chunks `text` reconstruit exactement la string que
  `chat()` aurait renvoyée (parse d'action sous-agent inchangé).
- `log_llm_call` + events debug de fin conservés (texte agrégé, tokens).

Démontrable : en mode SDK, le feed d'activité affiche le streaming token-par-token + le
reasoning en direct + le footer perf, identiques au transport OpenAI.

## Acceptance criteria

- [ ] `stream_chat()` en mode SDK streame via `respond_stream()` et yield des
      `StreamChunk` `text` + `reasoning` tick par tick.
- [ ] La concaténation des chunks `text` est byte-identique à la sortie de `chat()` pour
      la même entrée.
- [ ] Un chunk `perf` final porte TTFT, tok/s et les comptes de tokens issus des stats SDK.
- [ ] `schema` fourni → streaming guided-JSON réellement contraint.
- [ ] L'adapter de streaming (M5) est isolé et testé via un itérateur d'events scripté
      (text-only ; reasoning+text ; stats finales).
- [ ] `log_llm_call` + events debug de fin émis avec le texte agrégé et les tokens.

## Blocked by

- issues/0111-lmstudio-sdk-chat-poc-flag.md
