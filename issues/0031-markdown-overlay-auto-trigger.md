## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Overlay card markdown style "Notes" du mockup + déclenchement automatique sur réponses structurées / longues + dismiss multi-voies.

Créer `frontend/src/components/sphere/MarkdownOverlay.tsx`. Structure portée de `Design Mockup/overlay.jsx` `OverlayCard` + `NotesBody` (corner brackets `.ov-corner`, header `.ov-header` avec `BOB · SURFACING` + chip `MARKDOWN` + `REF · NTS-XXXX` + bouton X, footer `.ov-footer` avec actions `READ ALOUD` / `OPEN` / `DISMISS`). Body = `<article class="ov-md">` qui wrap `react-markdown` + `remark-gfm` avec une prop `components` qui assigne les classes mockup (`.md-h1`, `.md-h2`, `.md-h3`, `.md-p`, `.md-quote`, `.md-ul`, `.md-ol`, `.md-pre`, `.md-hr`, inline code, lien). Animation cross-fade in-place du body uniquement quand le contenu change (header reste).

Props : `content: string | null`, `onClose: () => void`. Si `content === null` → null. Sinon rend l'overlay centré au-dessus de la sphère (z-index > sphere, < dev controls).

Dismiss :
- `Esc` key (global listener)
- Bouton X dans le header
- Clic sur `.overlay-stage` backdrop (pas sur la card)

Modifier `SphereApp` pour observer le dernier message assistant terminé. Quand un message assistant arrive (event `messages` updated avec dernière entrée `role: 'assistant'` non vide), évaluer `shouldOverlayResponse(content)`. Si `true` → set `overlayContent = content`. Si `false` → garder la transcript line, ne pas ouvrir.

`MarkdownOverlay` ouvert → `TranscriptLine` masquée (déjà géré dans issue 0030).

Tests `MarkdownOverlay.test.tsx` :
- Rend un sample markdown contenant heading + list + code fence + table + blockquote + lien : DOM contient `.md-h1`, `.md-h2`, `.md-ul > li`, `.md-pre`, table avec `<th>/<td>`, `.md-quote`, `<a>`
- `Esc` keydown → `onClose` appelé
- Clic sur `.overlay-stage` backdrop → `onClose` appelé
- Clic à l'intérieur de `.overlay-card` → `onClose` PAS appelé
- Clic sur bouton X → `onClose` appelé
- `content === null` → composant rend `null`

Test d'intégration léger : envoyer dans le store un assistant message overlay-worthy → `MarkdownOverlay` ouvert. Envoyer un assistant message court → reste fermé.

## Acceptance criteria

- [ ] `MarkdownOverlay` rend la card avec header / body / footer mockup-styled
- [ ] `react-markdown` + `remark-gfm` configurés avec classes CSS mockup
- [ ] Tables GFM s'affichent correctement
- [ ] Esc / X / backdrop click ferment l'overlay
- [ ] Réponse longue ou structurée déclenche overlay auto
- [ ] Réponse courte plain → reste en transcript line, pas d'overlay
- [ ] Nouvelle réponse overlay-worthy alors qu'overlay déjà ouvert → cross-fade in-place du body
- [ ] Tests Vitest passent (≥ 8 assertions distinctes)
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

- `issues/0026-vitest-setup-overlay-heuristic.md`
- `issues/0030-input-field-transcript-line.md`
