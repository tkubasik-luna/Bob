## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Toggle mute TTS : petit icône bottom-right + raccourci clavier `M`.

Créer `frontend/src/components/sphere/MuteToggle.tsx`. Bouton glyph positionné en bottom-right (offset des frame corners, e.g. `position: absolute; bottom: 18px; right: 36px;`). Icon SVG haut-parleur + variante barrée. Style : couleur `--ink-dim`, hover `--ink`, taille ~18px. Pas de bord, transparent bg.

Wire avec le hook existant `useVoiceMode` (`voiceEnabled` / `toggle`). Clic → `toggle()`. Icon affiché reflète l'état (haut-parleur normal si `voiceEnabled`, haut-parleur barré sinon).

Raccourci global `M` : `useEffect` au niveau `SphereApp` qui attache un `keydown` listener. Si `e.key === 'm'` ou `'M'` ET `e.target.tagName !== 'INPUT'` ET `'TEXTAREA'` (skip si focus dans input — copy du pattern mockup) → `toggle()`. Cleanup le listener sur unmount.

Tests `MuteToggle.test.tsx` :
- Click button → `toggle` callback appelé
- État `voiceEnabled=true` → icon haut-parleur normal
- État `voiceEnabled=false` → icon haut-parleur barré
- `keydown` `M` global → `toggle` appelé
- `keydown` `M` quand focus dans `<input>` → `toggle` PAS appelé

## Acceptance criteria

- [ ] `MuteToggle` rend l'icône bottom-right avec style HUD
- [ ] Clic toggle `voiceEnabled` via `useVoiceMode`
- [ ] Icon swap (haut-parleur normal / barré) selon état
- [ ] Raccourci `M` toggle globalement
- [ ] Skip raccourci `M` quand focus dans input texte
- [ ] Tests Vitest passent
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

- `issues/0030-input-field-transcript-line.md`
