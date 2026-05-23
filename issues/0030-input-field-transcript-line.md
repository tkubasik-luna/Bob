## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Champ texte fixe en bas de l'écran + ligne de transcript fade-in/out au-dessus + dispatch via le WS existant. Permet de tenir une conversation complète user ↔ Bob dans la fenêtre `?ui=new`.

Créer `frontend/src/components/sphere/InputField.tsx` : input texte fixe en bas (zone `.hud-zone.b` modifiée pour héberger input + transcript empilés). Style HUD : border `--hud-rule`, font `Space Grotesk`, placeholder discret (`"Tapez pour parler à Bob"`), padding cohérent avec le mockup. `Enter` envoie via le hook `useWebSocket` existant (reuse exactement la même fonction de submit que `ChatView`). `Shift+Enter` insère une newline (input multi-ligne via `textarea` styled comme input). Value vidée après submit.

Créer `frontend/src/components/sphere/TranscriptLine.tsx` : composant zone bas (placé juste au-dessus de `InputField`). Affiche :
- Pendant `idle` initial (zéro message) : hint `"Tapez pour parler à Bob"` en `.hud-transcript-hint`
- Pendant `think` : derniers prompt user en italique discret OU les "thinking · · ·" dots du mockup (`hud-transcript-thinking`)
- Pendant / après `speak` : snippet du dernier assistant message (premier ~80 caractères, ellipsis si plus long) en `.hud-transcript-text`
- Caché (display:none) quand `MarkdownOverlay` est ouvert

Fade in/out via le pattern mockup (`key={state + '_' + (text ? 'on' : 'off')}` pour forcer re-mount + animation CSS).

Modifier `SphereApp` pour composer `<SphereCanvas /> + <TranscriptLine /> + <InputField />` dans le bon ordre (canvas plein écran, transcript + input dans `.hud-zone.b`).

Tests :
- `InputField.test.tsx` : tape texte + `Enter` → callback submit appelé avec la valeur, value cleared. `Shift+Enter` → ne submit pas, ajoute newline. Submit avec value vide → ne callback pas.
- `TranscriptLine.test.tsx` : affiche hint en idle/aucun message, affiche user prompt en think, affiche snippet assistant (truncated à 80 chars) en speak.

## Acceptance criteria

- [ ] `InputField` rend le textarea fixed bottom style HUD
- [ ] `Enter` sans modifier envoie le message via WS (reuse le path `ChatView` existant)
- [ ] `Shift+Enter` insère newline
- [ ] `TranscriptLine` affiche hint / user prompt / assistant snippet selon l'état
- [ ] Conversation complète possible dans la fenêtre `?ui=new` : taper, voir sphère think, voir réponse, recommencer
- [ ] `?ui=legacy` toujours intact
- [ ] Tests Vitest passent
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

- `issues/0029-use-sphere-state-derive.md`
