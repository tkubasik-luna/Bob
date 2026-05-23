## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Fenêtre Tauri `new` en mode borderless + drag region custom 28px top. Look cinématique sans chrome OS.

Modifier `frontend/src-tauri/tauri.conf.json` sur la fenêtre `new` (label `"new"`) :
- `"decorations": false`
- `"transparent": false` (background app reste opaque)
- Ne pas ajouter `titleBarStyle` (laisse défaut macOS, devient inactif sans decorations)

La fenêtre `legacy` reste inchangée (decorations OS natives conservées).

Ajouter dans `frontend/src/styles/hud.css` une CSS rule pour la drag region :
```css
.app .drag-region {
  position: fixed;
  top: 0; left: 0; right: 0;
  height: 28px;
  z-index: 100;
  -webkit-app-region: drag;
  -webkit-user-select: none;
  user-select: none;
}
.app .drag-region * { -webkit-app-region: no-drag; }
.app input, .app textarea, .app button { -webkit-app-region: no-drag; }
```

Modifier `SphereApp` pour rendre `<div className="drag-region" />` en haut de l'arbre (avant `SphereCanvas`).

Raccourcis OS standard : Tauri v2 gère nativement `Cmd+W` (close window), `Cmd+M` (minimize), `Cmd+Q` (quit app) sur macOS sans config supplémentaire même quand `decorations: false`. Pas de fallback custom à implémenter.

Tests `frontend/src-tauri/tauri.conf.test.ts` (lecture du JSON, assertions structure) ET tests CSS via DOM render :
- Parse `tauri.conf.json`, vérifier que la fenêtre label `new` a `decorations: false` ET la fenêtre label `legacy` n'a pas ce champ (ou `decorations: true`)
- Render `<SphereApp />` dans test : `.drag-region` est présent, a la CSS rule `-webkit-app-region: drag` appliquée (test via `getComputedStyle`)
- Render `<InputField />` : input intérieur a `-webkit-app-region: no-drag`

## Acceptance criteria

- [ ] Fenêtre Tauri `new` a `decorations: false` dans `tauri.conf.json`
- [ ] Fenêtre Tauri `legacy` conserve son chrome OS
- [ ] `.drag-region` rendue en haut de `SphereApp`, 28px haut, `-webkit-app-region: drag`
- [ ] Input / textarea / button ont `-webkit-app-region: no-drag` (pas d'absorption des clicks par le drag)
- [ ] Tests Vitest assertent la structure JSON Tauri + présence du DOM drag region
- [ ] `pnpm check` + `pnpm typecheck` passent
- [ ] `pnpm test` passe (tous les tests des slices précédentes encore verts)

## Blocked by

- `issues/0030-input-field-transcript-line.md`
- `issues/0034-mute-toggle.md`
