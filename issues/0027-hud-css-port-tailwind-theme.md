## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Port du stylesheet global du mockup + bridge tokens design Tailwind v4 + chargement des fonts.

Créer `frontend/src/styles/hud.css` qui copie verbatim le `<style>` de `Design Mockup/Bob - Sphere Lab.html` (toutes les CSS vars `--bg`, `--bg-2`, `--ink`, `--ink-dim`, `--ink-faint`, `--accent`, `--accent-2`, `--accent-3`, `--warn`, `--err`, `--hud-rule`, `--hud-rule-dim`, `--hud-fill`, `--font-sans`, `--font-mono`, ainsi que les sélecteurs `.theme-warm`, `.mood-calm`, `.state-alert`, `.state-error`, `.sphere-stage`, `.sphere-canvas`, `.glyph-overlay`, `.hud-frame`, `.hud-zone`, `.hud-identity`, `.hud-telemetry`, `.hud-tasks`, `.hud-staterail`, `.hud-tickscale`, `.hud-transcript`, `.hud-thoughts`, `.hud-diag`, `.overlay-stage`, `.overlay-card`, `.ov-*`, `.md-*`, `.state-pills`, `.surface-picker`, `.twk-*`, animations `@keyframes`).

Charger Google Fonts (`Space Grotesk`, `JetBrains Mono`, `Geist`, `Geist Mono`, `Newsreader`) via `<link>` dans `frontend/index.html` (preconnect + stylesheet).

Importer `hud.css` dans `frontend/src/main.tsx` après `index.css`.

Dans `frontend/src/index.css`, ajouter un bloc `@theme` Tailwind v4 qui mappe les CSS vars vers les tokens Tailwind (`--color-accent: var(--accent)`, `--color-bg: var(--bg)`, `--color-ink: var(--ink)`, `--color-hud-rule: var(--hud-rule)`, `--font-family-sans: var(--font-sans)`, `--font-family-mono: var(--font-mono)`, etc.).

Updater le placeholder `SphereUI` actuel pour appliquer les classes du mockup : conteneur racine `<div class="app theme-warm mood-calm state-idle surface-none">`. Le placeholder doit montrer le bon background warm `#0A0606` + une indication texte minimaliste centrée.

## Acceptance criteria

- [ ] `frontend/src/styles/hud.css` créé, port verbatim du mockup `<style>`
- [ ] Google Fonts liés dans `frontend/index.html` avec preconnect
- [ ] `hud.css` importé dans `main.tsx`
- [ ] `@theme` block dans `index.css` expose au moins `--color-accent`, `--color-bg`, `--color-ink`, `--font-family-sans`, `--font-family-mono`
- [ ] Une utility Tailwind du style `class="text-accent font-mono"` rend correctement avec les couleurs warm dans `?ui=new`
- [ ] `?ui=new` affiche le bg warm `#0A0606` + texte ink warm `#FFE7DD`
- [ ] `?ui=legacy` reste pixel-identique à avant (aucune régression visuelle)
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

None - can start immediately
