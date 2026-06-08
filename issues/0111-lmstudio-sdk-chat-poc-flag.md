## Parent

prd/0017-lmstudio-sdk-inference.md

## What to build

Tranche fondatrice (tracer-bullet le plus fin) : faire passer un `chat()` LM Studio par
le SDK `lmstudio` au lieu de l'API OpenAI, derrière un flag de transport.

End-to-end :
- Nouveau réglage `LLM_LMSTUDIO_TRANSPORT` (`sdk` | `openai`, défaut `openai`).
- Sélection du client dans la factory : `openai` → `LMStudioClient` actuel (inchangé) ;
  `sdk` → nouveau `LMStudioSDKClient`.
- `LMStudioSDKClient.chat(messages, schema, session_id)` : convertit les messages
  (système/user/assistant, fold `system_validator` issue 0048 appliqué avant conversion)
  en `Chat` SDK, appelle `model.respond(...)` sur un handle obtenu via
  `AsyncClient.llm.model(<model>)`, host dérivé de `LLM_BASE_URL` via `host_from_base_url`.
- `schema` fourni → `response_format` natif du SDK (structured output réellement gaté).
- Niveau `reasoning` per-rôle transmis via `config.raw` (`{"reasoning": level}`) ;
  `max_tokens` via la config SDK.
- Vrais comptes de tokens lus depuis les stats de prédiction ; events de debug
  (`llm_call_start`/`end`) + `log_llm_call` conservés ; garde « contenu vide » conservée ;
  erreurs `LMStudioError*` mappées en `LLMClientError`.

C'est le POC : démontrable en basculant le flag sur un LM Studio réel et en obtenant une
réponse de chat identique à celle du transport OpenAI.

## Acceptance criteria

- [ ] `LLM_LMSTUDIO_TRANSPORT` existe (config), défaut `openai`, et pilote la sélection
      du client LM Studio dans la factory.
- [ ] `LLM_LMSTUDIO_TRANSPORT=openai` reproduit le comportement actuel à l'identique
      (aucune régression).
- [ ] `LLM_LMSTUDIO_TRANSPORT=sdk` fait passer `chat()` par `model.respond()` du SDK,
      host dérivé de `LLM_BASE_URL`.
- [ ] `chat(schema=…)` produit une sortie réellement contrainte par le structured output
      natif du SDK.
- [ ] Le fold `system_validator` (issue 0048) est appliqué avant conversion ; l'assertion
      des rôles standard est conservée.
- [ ] Tokens in/out remontés depuis les stats SDK ; events debug + `log_llm_call` émis ;
      contenu vide → `LLMClientError` ; erreurs SDK → `LLMClientError`.
- [ ] Tests offline avec SDK faké : sélection par flag, parité de surface de `chat`,
      mapping schema, garde contenu vide, mapping d'erreurs.
- [ ] Validation manuelle hors-CI sur LM Studio réel : réponse de chat correcte ET parité
      du niveau `reasoning` via `config.raw` (sinon fallback documenté = omission).

## Blocked by

None - can start immediately.
