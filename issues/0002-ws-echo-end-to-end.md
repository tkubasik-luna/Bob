## Parent

`prd/0001-bob-mvp-foundation.md`

## What to build

Établir la plomberie WebSocket bidirectionnelle complète, sans LLM. L'utilisateur tape un message dans le front, le backend le reçoit, lui renvoie un echo, le front l'affiche dans l'historique conversation comme s'il s'agissait d'une réponse d'assistant.

Backend : ajouter un endpoint WS `GET /ws/chat`. À la connexion, émettre `{type: "session", session_id: <uuid>}`. À chaque `{type: "user_msg", content}` reçu, renvoyer `{type: "thinking", state: "start"}`, puis `{type: "assistant_msg", speech: <echo du content>, ui: []}`, puis `{type: "thinking", state: "end"}`. Cleanup propre à la déconnexion (purge state per-session).

Frontend : implémenter le hook `useWebSocket` qui wrappe l'API native `WebSocket`, gère reconnect avec backoff exponentiel, queue les messages envoyés pendant disconnect, expose `connectionStatus`. Implémenter le store Zustand `chatStore` (`messages: ChatMessage[]`, `connectionStatus`, `isWaitingResponse`, `sessionId`). Construire un `ChatView` minimaliste : header simple, zone scrollable d'historique avec bulles user vs assistant distinctes, textarea + bouton d'envoi en bas, Entrée envoie, Shift+Entrée newline, auto-scroll bottom sur nouveau message. Indicateur visuel quand `connectionStatus !== 'open'`. Indicateur "thinking" pendant l'attente.

Pas encore de registry de composants ni de Markdown : les bulles assistantes affichent juste `speech` en texte brut.

## Acceptance criteria

- [ ] Backend expose `GET /ws/chat` côté `127.0.0.1:8000`
- [ ] Connexion WS émet immédiatement `{type: "session", session_id}` avec un UUID
- [ ] Envoi `{type: "user_msg", content: "hello"}` produit dans l'ordre : `thinking start`, `assistant_msg` avec `speech: "hello"`, `thinking end`
- [ ] Déconnexion WS purge l'état serveur lié à cette session
- [ ] Hook `useWebSocket` reconnecte automatiquement après un kill du backend (backoff exponentiel)
- [ ] Messages envoyés pendant disconnect sont queuées et flushés à la reconnexion
- [ ] `chatStore` Zustand expose `messages`, `connectionStatus`, `isWaitingResponse`, `sessionId`
- [ ] `ChatView` affiche bulles user (alignées une side) vs assistant (autre side), avec styling Tailwind distinct
- [ ] Textarea en bas, Entrée envoie le message, Shift+Entrée insère newline
- [ ] Bouton "Envoyer" cliquable, disabled si input vide ou si déconnecté
- [ ] Auto-scroll bottom déclenché à chaque nouveau message ajouté
- [ ] Indicateur "déconnecté" visible quand `connectionStatus !== 'open'`
- [ ] Indicateur "thinking" (ex: dots animés) visible entre `thinking start` et `thinking end`
- [ ] Test manuel : taper "hello", voir "hello" apparaître comme réponse assistant
- [ ] Test manuel : kill backend, voir indicateur déconnexion, relancer backend, voir reconnexion auto

## Blocked by

- `issues/0001-scaffold-monorepo-tooling.md`
