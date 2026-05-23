## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Wrap-up final de la feature 0004. Pas de purge legacy, pas de flip de default. Les 2 fenêtres Tauri (`Bob · Legacy` + `Bob · Sphere`) restent telles quelles. `ChatView` et toute la chain legacy restent intacts, accessibles via `?ui=legacy`. `debug.html` reste disponible.

L'objectif de cette issue : verrouiller la feature, documenter, et s'assurer que l'intégration globale est verte.

Créer `docs/features/0004-sphere-hud-ui.md` suivant le pattern des 3 features déjà shipped (cf. `docs/features/0003-jarvis-orchestrator.md`). Contenu attendu : brève synthèse du périmètre livré, captures éventuelles, mapping vers `prd/0004-sphere-hud-ui.md`, liste des issues consommées (`0026` → `0036`), notes de migration (aucune côté backend, frontend = nouvelle UI opt-in via fenêtre `Sphere`).

Modifier `CLAUDE.md` racine : ajouter dans la section "Shipped features" une ligne :
- `[0004 Sphere HUD UI](docs/features/0004-sphere-hud-ui.md) — Sphère WebGL + HUD minimal (tasks panel, transcript line, markdown overlay) en fenêtre Tauri séparée, cohabitant avec ChatView legacy.`

Vérifier l'intégration full :
- `pnpm typecheck` passe
- `pnpm check` passe
- `pnpm test` passe (toute la suite verte de 0026 → 0036)
- `./scripts/dev.sh` ouvre toujours 2 fenêtres Tauri (`Bob · Legacy` 900×700 + `Bob · Sphere` 1280×800)
- `Bob · Legacy` rend `ChatView` actuel sans régression
- `Bob · Sphere` rend la nouvelle UI complète (sphère + tasks + transcript + input + overlay si réponse longue + mute + dev mode si `dev=1`)
- `Design Mockup/` reste intact

Pas de modif `App.tsx`, pas de modif `tauri.conf.json`, pas de suppression de fichiers, pas de modif `scripts/dev.sh`, pas de modif `frontend/public/debug.html`.

## Acceptance criteria

- [ ] `docs/features/0004-sphere-hud-ui.md` créé, suit le pattern des features précédentes
- [ ] `CLAUDE.md` racine mentionne `0004 Sphere HUD UI` dans Shipped features
- [ ] `pnpm typecheck` + `pnpm check` + `pnpm test` passent
- [ ] `./scripts/dev.sh` ouvre les 2 fenêtres Tauri sans erreur
- [ ] `?ui=legacy` rend ChatView intact (aucune régression depuis avant 0004)
- [ ] `?ui=new` rend la Sphere UI complète (sphère + tasks panel + input + transcript + overlay si applicable + mute)
- [ ] `?ui=new&dev=1` révèle state pills + tweaks panel
- [ ] Aucun fichier legacy supprimé (ChatView, ChatMessageBlock, TaskCard, TaskSidebar, TaskDrawer, Dispatcher, registry, MarkdownView, Toast, SphereUI placeholder tous présents)
- [ ] `tauri.conf.json` reste avec 2 fenêtres `legacy` + `new`
- [ ] `debug.html` reste dans `frontend/public/`
- [ ] `Design Mockup/` intact

## Blocked by

- `issues/0031-markdown-overlay-auto-trigger.md`
- `issues/0032-hud-tasks-panel.md`
- `issues/0033-audio-level-sphere-reactivity.md`
- `issues/0034-mute-toggle.md`
- `issues/0035-dev-controls-gated.md`
- `issues/0036-tauri-borderless-drag-region.md`
