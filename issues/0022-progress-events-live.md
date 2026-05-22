## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Troisième action sub-agent : `progress(status)`. Permet au sub-agent d'émettre un statut intermédiaire lisible (ex: "j'analyse le 3e document sur 10") sans terminer la task. La card sidebar affiche ce statut en live.

Le sub-agent peut émettre `progress` plusieurs fois consécutives durant son loop, puis termine forcément avec `done` ou `ask_user`. Un cap dur de 10 iterations sans `done`/`ask_user` empêche les boucles infinies (état `failed` avec reason="max_iterations").

À la fin du slice : sub-agent qui pense longtemps peut tenir l'utilisateur informé via la sidebar sans spammer le chat principal (les progress events ne déclenchent PAS de message proactif Jarvis).

## Acceptance criteria

- [ ] `SubAgentRunner` loop supporte action `progress(status)` : persist message avec action=progress, émet event `task_message_added`, ré-itère immédiatement (pas de transition state — reste `running`).
- [ ] Cap de 10 progress consécutifs sans done/ask_user → task → `failed` avec reason="max_iterations_exceeded".
- [ ] WS event `task_updated` étendu avec champ optionnel `progress_status: string`.
- [ ] `ProactivityHandler` n'est **PAS** trigger sur progress events (silencieux côté Jarvis).
- [ ] `TaskCard` sidebar affiche `progress_status` sous le titre quand présent (italique, gris, tronqué).
- [ ] Tests `SubAgentRunner` : mock LLM séquence `progress`×3 puis `done` → 3 events progress, 1 done, status visible dans messages ; mock LLM séquence `progress`×11 → fail avec reason.

## Blocked by

- issues/0021-multi-turn-ask-user-forward.md
