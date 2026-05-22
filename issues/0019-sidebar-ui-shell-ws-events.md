## Parent

prd/0003-jarvis-orchestrator.md

## What to build

UI sidebar à droite qui rend visibles les sous-tâches au fur et à mesure qu'elles vivent. Layout `ChatView` split en deux : chat Jarvis ~70% à gauche, `TaskSidebar` ~30% à droite. Cards minimalistes (titre + icon état + couleur), une par task active.

Côté backend, ajout des WS events qui poussent les changements d'état au frontend. Côté frontend, nouveau slice Zustand `tasks` qui ingère ces events et alimente la sidebar en temps réel.

À la fin du slice : demo possible. User dit à Jarvis de déléguer une tâche, sidebar affiche immédiatement une card "pending" qui passe à "running" puis "done" sans refresh.

## Acceptance criteria

- [ ] WS server-to-client events ajoutés : `task_created` `{task_id, title, goal, state, created_at}`, `task_updated` `{task_id, state, needs_attention, updated_at}`, `task_result` `{task_id, result}`.
- [ ] Backend émet ces events depuis `Orchestrator` (sur spawn) et `SubAgentRunner` (sur transitions).
- [ ] `ws.ts` frontend : nouveaux variants typés + handlers `useWebSocket`.
- [ ] Zustand `tasks` slice : map `task_id → Task`, upsert sur chaque event.
- [ ] `TaskSidebar` component : vertical list de `TaskCard`, sticky header "Tâches en cours", scrollable.
- [ ] `TaskCard` minimal : icon état (couleur par state : gris=pending, bleu=running, jaune=waiting_input, vert=done, rouge=failed), titre tronqué si long, timestamp.
- [ ] `ChatView` layout split : chat centre + sidebar droite (responsive width).
- [ ] Au connect WS, le backend envoie la liste des tasks actives existantes (`task_created` pour chacune) afin que la sidebar soit reconstruite au reload.
- [ ] Smoke test manuel documenté : spawn une task, observer card apparaître et transitionner.

## Blocked by

- issues/0018-first-task-spawn-end-to-end.md
