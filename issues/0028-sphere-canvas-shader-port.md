## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Port du renderer WebGL2 + sphère visible au centre de la fenêtre `?ui=new`.

Porter `Design Mockup/sphere-shader.js` vers `frontend/src/sphere/sphereShader.ts`. Conserver les 6 variants (`liquid`/`swarm`/`wire`/`plasma`/`void`/`glyph`) et les 6 state overlays (`idle halo`/`listen wave`/`think swirl`/`speak pulse`/`alert tint`/`error glitch`) compilés dans le fragment shader. Exporter `createSphereRenderer(canvas: HTMLCanvasElement)` avec interface `{ setSize, render }`. Typer strictement TS.

Porter `Design Mockup/sphere.jsx` vers `frontend/src/sphere/SphereCanvas.tsx`. Props : `state: 'idle'|'listen'|'think'|'speak'|'alert'|'error'`, `variant: number`, `motion: number`, `glow: number`, `theme: 'warm'|'cold'`, `mood: 'calm'|'normal'`, `audioLevel?: number`. Conserver la crossfade state weights (`~250ms`) et l'interpolation de couleurs. Conserver le glyph overlay canvas2D (variant 5) — appelé même si invisible en V1.

V1 props lockées dans `SphereApp` : `variant={0}`, `theme="warm"`, `mood="calm"`. `motion={0.55}`, `glow={0.7}` par défaut (mockup TWEAK_DEFAULTS).

Si `WebGL2` indispo, le component affiche une bannière d'erreur HUD-style (`<div class="hud-error">WebGL2 required — open this app in a Chromium / WebKit recent build</div>`) au lieu de crasher.

Mettre à jour `SphereUI` placeholder pour rendre `<SphereCanvas {...lockedProps} state="idle" />` plein écran via la classe `.app theme-warm mood-calm state-idle`.

Tests dans `frontend/src/sphere/SphereCanvas.test.tsx` :
- Mounte sans erreur avec props valides
- Renderer initialisé sur mount (mock `getContext('webgl2')` qui retourne un stub avec les méthodes nécessaires + flag `__renderCalls`)
- `cancelAnimationFrame` appelé sur unmount
- Bannière d'erreur affichée si `getContext('webgl2')` retourne `null`

## Acceptance criteria

- [ ] `frontend/src/sphere/sphereShader.ts` exporte `createSphereRenderer`
- [ ] `frontend/src/sphere/SphereCanvas.tsx` composant React typé strict
- [ ] `?ui=new` affiche une sphère liquid mercury qui respire au centre, palette warm, mood calm
- [ ] Resize de la fenêtre Tauri `new` rescale le canvas (DPR ≤ 2)
- [ ] Si WebGL2 indispo, bannière d'erreur lisible (pas écran noir)
- [ ] Tests Vitest passent (renderer mounté, cleanup, fallback erreur)
- [ ] `pnpm check` + `pnpm typecheck` passent
- [ ] `?ui=legacy` toujours intact

## Blocked by

- `issues/0027-hud-css-port-tailwind-theme.md`
