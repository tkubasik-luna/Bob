## Parent

`prd/0001-bob-mvp-foundation.md`

## What to build

Brancher `chat_service` derrière le WS, et côté front rendre dynamiquement les composants `ui[]` reçus du backend. Premier flux complet end-to-end : utilisateur tape un message → LM Studio génère une réponse JSON validée → front affiche `speech` + `ui[]` rendus via le component registry.

Backend : remplacer la logique d'echo du `ws_router` (slice 2) par un appel à `chat_service.handle_user_message(session_id, content)`. À chaque message utilisateur :
1. Émettre `{type: "thinking", state: "start"}`
2. Appeler `chat_service.handle_user_message(...)`
3. Émettre `{type: "assistant_msg", speech, ui}` avec le `ParsedResponse`
4. Émettre `{type: "thinking", state: "end"}`

Disconnect → `conversation.clear(session_id)`.

Frontend : implémenter `componentRegistry` (TypeScript `Record<string, React.ComponentType<any>>`) qui mappe `"ChatMessage"` et `"Markdown"` à leurs composants React. `Markdown` utilise `react-markdown` + `remark-gfm`. `ChatMessage` rend une bulle stylée selon `role`.

Implémenter `Dispatcher` : composant React qui prend `ui: ComponentDescriptor[]` et rend chaque composant via lookup dans `componentRegistry`. Composant inconnu → fallback `<UnknownComponent name={name} />` qui affiche un warning visuel mais ne crashe pas.

Modifier `ChatView` : pour chaque message assistant, rendre d'abord la bulle avec `speech` (toujours visible) puis, en dessous, le résultat de `<Dispatcher ui={message.ui} />`. Les messages user restent une bulle simple.

Types TS dérivés du contrat WS (manuellement, pas de codegen V0).

## Acceptance criteria

- [ ] `ws_router` appelle `chat_service.handle_user_message(...)` au lieu d'echo
- [ ] Séquence émise par tour : `thinking start` → `assistant_msg{speech, ui}` → `thinking end`
- [ ] Disconnect WS déclenche `conversation.clear(session_id)`
- [ ] Front `componentRegistry` mappe `ChatMessage` et `Markdown` à des composants React
- [ ] `Markdown` rend GFM (gras, italique, listes, code, liens) via `react-markdown` + `remark-gfm`
- [ ] `Dispatcher` rend la liste `ui[]` en lookup registry
- [ ] Composant inconnu → fallback `UnknownComponent` (warning visuel, pas crash)
- [ ] `ChatView` rend pour chaque message assistant : bulle `speech` + `<Dispatcher ui={ui} />`
- [ ] Test manuel avec LM Studio + Qwen 7B : taper "salut" → bulle assistant avec `speech` non vide affichée
- [ ] Test manuel : demander "donne-moi un exemple de liste en markdown dans un composant Markdown" → un `Markdown` dans `ui[]` rend correctement (validation que le LLM dispatche bien)
- [ ] Test manuel multi-tour : "mon prénom est Tom" → "comment je m'appelle ?" → réponse mentionne Tom
- [ ] Test manuel deux instances Tauri ouvertes simultanément → conversations isolées
- [ ] `ruff`, `mypy strict`, `biome`, `tsc --noEmit`, `pytest` passent

## Blocked by

- `issues/0002-ws-echo-end-to-end.md`
- `issues/0005-conversation-chat-service.md`
