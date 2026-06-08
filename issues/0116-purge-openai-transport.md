## Parent

prd/0017-lmstudio-sdk-inference.md

## What to build

Phase finale (M8) : atteindre l'état « SDK partout » en retirant le transport OpenAI pour
LM Studio et la dépendance `openai`, une fois le transport SDK validé en usage réel.

End-to-end :
- Basculer le défaut de `LLM_LMSTUDIO_TRANSPORT` sur `sdk`, puis retirer le flag et la
  branche `openai` de la factory (LM Studio passe toujours par le SDK).
- Supprimer `LMStudioClient` (transport OpenAI) et la dépendance `openai` du projet.
- Nettoyer le codec natif OpenAI s'il devient mort (le codec Hermes pour Claude CLI reste).
- Ajuster `LLM_TOOL_MODE` : ne garder que ce qui sert encore à Claude CLI (hermes) ; les
  réglages OpenAI-spécifiques (`LLM_API_KEY`, suffixe `/v1`) sont nettoyés/documentés.
- Mettre à jour la doc (README, CLAUDE.md le cas échéant) pour refléter « SDK partout ».

Démontrable : le projet n'importe plus `openai` ; toute l'inférence LM Studio passe par le
SDK ; la suite de tests reste verte.

## Acceptance criteria

- [ ] `LMStudioClient` (transport OpenAI) et la dépendance `openai` supprimés ; plus aucun
      import `openai` dans le code.
- [ ] Le flag `LLM_LMSTUDIO_TRANSPORT` est retiré (ou figé `sdk`) ; la factory ne construit
      plus que le client SDK pour LM Studio.
- [ ] Le codec natif OpenAI mort est retiré ; le codec Hermes (Claude CLI) intact.
- [ ] `LLM_TOOL_MODE` et les réglages résiduels OpenAI nettoyés/documentés.
- [ ] Toute la suite de tests passe sans la dépendance `openai`.
- [ ] Doc à jour (README / CLAUDE.md) : inférence LM Studio = SDK `lmstudio`.

## Blocked by

- issues/0112-lmstudio-sdk-stream-chat.md
- issues/0113-lmstudio-sdk-complete-tool-capture.md
- issues/0114-lmstudio-sdk-stream-complete-args-override.md
- issues/0115-lmstudio-sdk-client-lifecycle-swap.md
