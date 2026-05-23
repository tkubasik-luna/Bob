## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Panel "tâches en cours" top-right bindé sur `chatStore.tasks` (vrais Jarvis sub-tasks).

Créer `frontend/src/components/sphere/HudTasks.tsx`. Port direct de `Design Mockup/hud.jsx` `HUDTasks` (header `TÂCHES · ARRIÈRE-PLAN` + count `XX/XX`, liste de cards `.hud-task`). Adapter pour lire `useChatStore(s => s.tasks)` au lieu des mock auto-cycling tasks.

Mapping store → format mockup :
- `state: 'pending' | 'queued'` → CSS class `is-queued`, sub texte `EN FILE`, progress hidden
- `state: 'running'` → CSS class `is-running`, sub texte `${progress * 100} %` (utilise `progressStatus` si présent comme override texte, ex: "j'analyse 3/10"), spinner arc tournant
- `state: 'done'` → CSS class `is-done`, sub texte `OK`, check icon, fade-out après ~3s
- `state: 'failed'` → CSS class `is-error`, sub texte `ÉCHEC`, cross icon
- `state: 'waiting_input'` → traiter comme `is-queued` mais sub texte `ATTENTE INPUT` + indicateur `needsAttention` (border accent ou pulse)

Le champ `progress` n'existe pas dans `Task` actuel (le store n'a que `progressStatus` string). V1 : utiliser un proxy visuel — pour `running` sans `progress` numérique, afficher la barre `.hud-task-prog` en mode "indeterminate" (animation slide left-to-right). Si `progressStatus` est défini, l'afficher en sub texte au lieu du `XX %`.

Limite affichage : `slice(-4)` comme mockup (4 dernières). Si zéro task : panel rendu vide (juste le header avec `00/00`) — pas caché. Header anim `tasks-in` joué une seule fois au mount.

Modifier `SphereApp` pour placer `<HudTasks />` dans `.hud-zone.tr`.

Tests `HudTasks.test.tsx` :
- Aucune task → header affiche `00/00`, liste vide
- 1 task running → card avec `.is-running`, spinner DOM présent, sub texte affiche progress status si défini
- 1 task done → card avec `.is-done`, check icon, sub `OK`
- 1 task failed → `.is-error`, cross, sub `ÉCHEC`
- 5 tasks → affiche 4 (les dernières)
- Count badge affiche `running/total` avec class `is-live` si `running > 0`

## Acceptance criteria

- [ ] `HudTasks` rend la panel top-right mockup-styled
- [ ] Bind direct sur `useChatStore.tasks`, pas de mock
- [ ] Spawn d'une vraie sub-task Jarvis (via prompt user) fait apparaître une card avec spinner
- [ ] Task done → animation done + fade-out
- [ ] Task failed → croix rouge
- [ ] `progressStatus` du store affiché si présent
- [ ] Limite 4 cards visibles
- [ ] Tests Vitest passent
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

- `issues/0027-hud-css-port-tailwind-theme.md`
