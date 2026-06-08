## Parent

prd/0017-lmstudio-sdk-inference.md

## What to build

Lifecycle des clients SDK (M7) : un `AsyncClient` long-vécu **par rôle**, reconstruit au
swap de modèle/host, avec reconnexion résiliente — pour amortir le handshake websocket et
garder un TTFT bas, tout en restant pilotable à chaud par le picker.

End-to-end :
- Chaque `LMStudioSDKClient` de rôle détient un `AsyncClient` SDK long-vécu (lazy-connect,
  websocket persistant), host dérivé de `LLM_BASE_URL`.
- Intégration au coordinateur de swap (`RoleLLMSwitcher`) : au changement de modèle/host
  d'un rôle, l'ancien client est fermé (`aclose`) et un nouveau est construit, sans toucher
  les autres rôles.
- Reconnexion : sur chute du websocket en cours d'appel (`LMStudioWebsocketError`),
  reconnexion + retry une fois avant de remonter une `LLMClientError`.
- Construit au boot pour les rôles LM Studio ; les rôles Claude CLI restent inchangés.

Démontrable : changer de modèle pour un rôle via le picker reconstruit son client SDK et
les appels suivants ciblent le nouveau modèle ; une coupure transitoire du serveur est
absorbée par le retry.

## Acceptance criteria

- [ ] Chaque rôle LM Studio en mode SDK détient un `AsyncClient` long-vécu (pas de
      reconnexion par appel).
- [ ] Un swap de modèle/host (via `RoleLLMSwitcher`) ferme l'ancien client et en
      reconstruit un, sans impacter les autres rôles.
- [ ] Une chute de websocket en cours d'appel déclenche reconnexion + retry une fois ;
      échec persistant → `LLMClientError`.
- [ ] Le picker LLM (REST `/api/llm/*`, HUD) continue de fonctionner à l'identique en mode
      SDK.
- [ ] Les rôles Claude CLI sont inchangés.
- [ ] Tests offline avec faux `AsyncClient` : reconstruction au swap, reconnect+retry-once
      sur websocket simulé tombé.

## Blocked by

- issues/0111-lmstudio-sdk-chat-poc-flag.md
