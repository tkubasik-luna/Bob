## Parent

prd/0011-agent-activity-feed.md

## What to build

Le feed reste lisible et performant quand plusieurs agents streament en même
temps (Jarvis + N sub-tasks concurrentes via le `TaskScheduler`).

- **Frontend store** : `activityFeedStore` gère plusieurs `agent_ref` concurrents
  comme des **lanes distinctes** ; les deltas d'un agent n'écrasent pas ceux d'un
  autre.
- **Throttling** : coalescing des `reasoning_delta` par tick d'animation (ou côté
  émission) pour borner le débit WS et le coût de re-render React sous
  concurrence.
- Chaque lane garde son identité ; pas d'entrelacement des textes de reasoning de
  deux agents.

## Acceptance criteria

- [ ] Avec 2-3 agents streamant simultanément, chaque bloc affiche uniquement son
      propre reasoning (pas de mélange).
- [ ] Les deltas sont coalescés par tick (pas un re-render par token).
- [ ] Tests store : agrégation correcte par `agent_ref` sous deltas entrelacés en
      entrée.

## Blocked by

- issues/0069-reasoning-stream-tracer.md
