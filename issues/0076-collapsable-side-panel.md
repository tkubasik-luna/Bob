## Parent

prd/0011-agent-activity-feed.md

## What to build

Le feed devient une surface intégrante de la HUD : un **panneau latéral
collapsable** qui remplace le `TaskSidebar`.

- **Frontend** : `AgentActivityPanel` — panneau latéral toujours présent,
  collapsable en **rail étroit** affichant des badges des agents actifs + un
  compteur. Auto-dépli quand une activité démarre. La sphère reste centrale.
- Le panneau héberge les `AgentBlock` (Jarvis + sub-tasks), réutilisant l'état du
  `chatStore` (tasks) comme source d'état.
- **Suppression du `TaskSidebar`** (et `TaskCard`, absorbé par `AgentBlock`).
- **HITL** : l'intégration visuelle (emprise vs sphère, rail, transitions,
  cohabitation mode voix) nécessite une revue de design avant merge — pas de
  maquette Figma fournie.

## Acceptance criteria

- [ ] Le `AgentActivityPanel` remplace le `TaskSidebar` dans la HUD.
- [ ] Le panneau se collapse en rail étroit (badges agents actifs + compteur).
- [ ] Le panneau se déplie automatiquement quand une activité démarre.
- [ ] La sphère reste centrale et lisible au repos (panneau collapsé).
- [ ] Le feed coexiste avec la parole TTS / sphère en mode voix.
- [ ] Revue de design validée par l'utilisateur (HITL).

## Blocked by

- issues/0072-jarvis-block.md
- issues/0074-block-lifecycle-collapse.md
