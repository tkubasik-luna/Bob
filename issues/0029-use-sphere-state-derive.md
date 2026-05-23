## Parent

`prd/0004-sphere-hud-ui.md`

## What to build

Hook pure `useSphereState` qui dérive l'état de la sphère depuis le store + binding sur `SphereCanvas`.

Créer `frontend/src/sphere/useSphereState.ts`. Le hook lit du `chatStore` : `connectionStatus`, `isWaitingResponse`, `speakingMsgId`, et la dernière entrée de `messages` (pour détecter "stream assistant en cours" — flag dérivé sur `messages[last].role === 'assistant'` ET `isWaitingResponse === false` ET stream non terminé ; concrètement, V1 ≈ pendant que `speakingMsgId` est `null` mais le dernier assistant message vient d'arriver, traiter comme `speak` brièvement OU se contenter d'utiliser `speakingMsgId` comme seul signal `speak`). Retourne `'idle' | 'think' | 'speak' | 'error'`.

Priorité de dérivation :
1. `connectionStatus !== 'open'` → `'error'`
2. `isWaitingResponse === true` → `'think'`
3. `speakingMsgId !== null` → `'speak'`
4. Sinon → `'idle'`

Modifier `SphereApp` (ex-`SphereUI` placeholder) pour appeler `useSphereState()` et passer son résultat à `SphereCanvas` via la prop `state`. Le wrapper `.app` reçoit aussi la classe `state-{value}` pour les overrides CSS (e.g. `.state-alert`, `.state-error` qui retintent les CSS vars).

Tests `frontend/src/sphere/useSphereState.test.ts` (avec `renderHook`) :
- Table-driven : chaque couple `(connectionStatus, isWaitingResponse, speakingMsgId)` → état attendu
- Transitions : `idle` → `think` quand `setWaiting(true)`
- `error` overrides tout : même si `isWaitingResponse=true`, si `connectionStatus='disconnected'` → `'error'`
- Fournir un store fake via `zustand` create (pas mocker `useChatStore` global — préférer injection de selector si plus propre)

## Acceptance criteria

- [ ] `frontend/src/sphere/useSphereState.ts` exporte le hook
- [ ] Hook pure (pas de side-effect, lit uniquement le store)
- [ ] `SphereApp` consume le hook + passe la valeur à `SphereCanvas`
- [ ] Wrapper `.app` reçoit `state-{value}` class
- [ ] Forcer manuellement `useChatStore.setState({ isWaitingResponse: true })` dans devtools fait basculer la sphère vers `think` visuellement
- [ ] Forcer `setStatus('disconnected')` → sphère bascule `error` avec glitch
- [ ] Tests Vitest couvrent au minimum les 4 états + 3 transitions
- [ ] `pnpm check` + `pnpm typecheck` passent

## Blocked by

- `issues/0028-sphere-canvas-shader-port.md`
