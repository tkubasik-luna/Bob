## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Module `TaskScheduler` qui centralise la concurrence : cap dur de 3 sous-tâches `running` simultanées, les autres queuées en `pending`. Promotion automatique d'une `pending` dès qu'une `running` termine (done/failed).

Aujourd'hui (post slice #4) le `SubAgentRunner` est lancé directement à chaque spawn. Avec ce slice, l'orchestrateur passe par le scheduler qui décide : "slot libre → run immédiat" ou "queue en pending".

À la fin du slice : si l'utilisateur spawn 5 tasks rapidement, la sidebar affiche 3 cards `running` et 2 `pending`. Dès qu'une `running` passe `done`, une `pending` passe automatiquement à `running`.

## Acceptance criteria

- [ ] `TaskScheduler.enqueue(task_id)` : si slot libre → promote `running` + lance `SubAgentRunner` ; sinon laisse `pending`.
- [ ] `TaskScheduler.on_task_terminated(task_id)` : décrémente compteur + promote la plus ancienne `pending`.
- [ ] `MAX_RUNNING_TASKS` lu depuis config (default 3).
- [ ] Au boot, scan SQLite : tasks `running` au moment du crash précédent sont remises en `pending` puis re-promues respectant le cap.
- [ ] `Orchestrator.spawn_subtask` route obligatoirement via le scheduler (pas de bypass).
- [ ] WS event `task_updated` émis quand une task passe `pending` → `running` via promotion.
- [ ] Sidebar : cards `pending` distinctes visuellement (icon hourglass ou couleur grise + texte "En attente").
- [ ] Tests : enqueue 5 avec cap=3 → 3 running, 2 pending ; on_task_terminated → premier pending promu ; cap respecté sous bursts asyncio concurrents ; reload après simulated crash respecte cap.

## Blocked by

- issues/0018-first-task-spawn-end-to-end.md
- issues/0019-sidebar-ui-shell-ws-events.md
