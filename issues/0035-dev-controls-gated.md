## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Dev controls gated derrière `?dev=1` : state pills + tweaks panel + raccourcis force-state. Persistance localStorage.

Créer `frontend/src/components/sphere/DevControls.tsx`. Composant rend uniquement si `new URLSearchParams(location.search).get('dev') === '1'`.

Contenu :
- **State pills** (port mockup `.state-pills`) : 6 boutons `idle` / `listen` / `think` / `speak` / `alert` / `error` (keys 1-6). Click force le state (override de la dérivation `useSphereState`). Pill actif = `.on` class avec accent.
- **Tweaks panel** (port `TweaksPanel` du mockup `tweaks-panel.jsx` — adapter en TSX) : sliders `motion` (0..1), `glow` (0..1) ; selects `state`, `variant` (0..5 noms `liquid/swarm/wire/plasma/void/glyph`), `mood` (`calm`/`normal`), `theme` (`warm`/`cold`) ; toggle `autoCycle` (cycle des 6 states toutes les 4.5s).
- **Keyboard shortcuts** : touches `1`-`6` force state (mappage mockup). Touches `7`-`9`/`a`-`f` skip (pas de surface picker en V1). Skip si `INPUT`/`TEXTAREA` focus.

Override de l'état : `DevControls` expose les valeurs via un store léger (nouveau Zustand slice `devTweaksStore` OU context React local). `SphereApp` lit ces valeurs et :
- Si dev override state est `null` → utilise `useSphereState()` (production behavior)
- Sinon → utilise le state forcé
- Motion / glow / variant / mood / theme overrides remplacent les locked defaults

Persistance : tous les tweaks (sauf state pill actif transitoire) écrits dans `localStorage.bob_dev_tweaks` au change, restaurés au mount.

AutoCycle : `useEffect` qui démarre un setInterval (4500ms) qui force-state cycle parmi les 6. Stop au toggle off.

Tests `DevControls.test.tsx` :
- Sans `?dev=1` → composant rend `null`
- Avec `?dev=1` (mock `window.location.search`) → state pills + tweaks panel rendus
- Click pill `think` → callback `setForcedState('think')` appelé
- Slide motion à 0.3 → store/callback reçoit 0.3
- localStorage : changer motion puis re-monter → motion restauré
- Keyboard `3` → state forcé à `think`
- Keyboard `3` quand focus dans `<input>` → skip

## Acceptance criteria

- [ ] `?ui=new` sans `dev` → aucun control visible
- [ ] `?ui=new&dev=1` → state pills + tweaks panel visibles
- [ ] Click pill change l'état sphère immédiatement
- [ ] Slider motion change la vitesse de respiration sphère
- [ ] Select variant change la variante (toutes 6 fonctionnelles)
- [ ] Select mood switch désature/resature
- [ ] Select theme switch warm/cold
- [ ] autoCycle toggle = sphere passe en boucle sur 6 states
- [ ] Persistance localStorage : reload garde les tweaks
- [ ] Raccourcis 1-6 force-state, skip dans input
- [ ] Tests Vitest passent
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

- `issues/0029-use-sphere-state-derive.md`
