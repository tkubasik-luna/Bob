## Parent

prd/0011-agent-activity-feed.md

## What to build

Cycle de vie d'un bloc quand sa task termine (done / failed) : il passe d'actif à
collapsé en résumé, tout en gardant l'accès au détail et au résultat.

- **Frontend** : à réception de `task_updated` (état terminal) / `task_result`,
  le `AgentBlock` collapse en résumé : titre + état final + bouton « résultat » +
  dépli « relire la réflexion ».
- Le bouton résultat ouvre l'**overlay existante** (Mail / Sections) via le
  `result_payload` déjà transporté.
- Le dépli ré-affiche la réflexion complète + les chips de ce bloc.
- `activityFeedStore` conserve le contenu nécessaire au collapse/expand.

## Acceptance criteria

- [ ] À la fin d'une task, son bloc collapse en résumé (titre + état + bouton
      résultat + dépli).
- [ ] Le bouton résultat ouvre l'overlay Mail/Sections appropriée.
- [ ] Le dépli ré-affiche la réflexion complète et les chips du bloc.
- [ ] Un échec (failed / cap / stall force-terminate) collapse avec l'état
      correct et l'incident visible.

## Blocked by

- issues/0069-reasoning-stream-tracer.md
- issues/0071-activity-chips-projector.md
