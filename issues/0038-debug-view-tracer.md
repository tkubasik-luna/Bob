## Parent

prd/0005-debug-view.md

## What to build

Premier tracer E2E de la vue debug : presser `Cmd+Shift+D` depuis la fenêtre Sphere ouvre une fenêtre Tauri dédiée qui affiche, en temps réel, chaque message envoyé par l'utilisateur comme une ligne lisible.

Périmètre minimum end-to-end :

- Backend : nouveau module `debug_log.py` qui expose `DebugEvent` (envelope `{ts, category, severity, source, summary, payload, turn_id, correlation_id?, replayed}`), un ring buffer mémoire (`deque(maxlen=2000)`), le helper `emit_debug(...)`, et un async generator `subscribe()` qui yield d'abord le snapshot du buffer (events taggés `replayed=True`) puis stream les nouveaux events à la demande. Pas de ContextVar `turn_id` ni de structlog bridge à ce stade — `turn_id` est passé en argument ou laissé `None`.
- Backend : nouveau module `ws_debug.py` qui déclare la route WebSocket `/ws/debug`, consomme `subscribe()` et stream chaque event en JSON. Enregistré dans `main.py` à côté du `/ws` existant.
- Backend : un seul appel `emit_debug(category="input", severity="info", source="orchestrator.process_user_message", summary=f"User envoie: \"{content[:80]}\"", payload={"content": content})` à l'entrée de `process_user_message` dans `orchestrator.py`.
- Frontend : entrée fenêtre `debug` dans `frontend/src-tauri/tauri.conf.json` avec `visible: false`, `width: 1024`, `height: 700`, `url: "/?ui=debug"`, `title: "Bob · Debug"`.
- Frontend : commande Tauri `toggle_debug_window` dans `frontend/src-tauri/src/main.rs` qui récupère la `WebviewWindow` par label `debug` et toggle `.show()` / `.hide()`. Enregistrée dans le builder.
- Frontend : `App.tsx` route `?ui=debug` vers un nouveau composant `<DebugView />`.
- Frontend : nouveau composant `DebugView.tsx` qui consomme un nouveau hook `useDebugWs.ts` et rend une liste verticale simple. Chaque ligne affiche `[HH:MM:SS.mmm] [category] summary` en monospace, newest en bas. Aucune toolbar, aucun filtre, aucun expand, aucun auto-scroll intelligent — juste un `<div>` qui scroll au bottom à chaque nouvel event via `useEffect`.
- Frontend : `useDebugWs.ts` ouvre la WS `/ws/debug`, parse les messages JSON en `DebugEvent[]`, expose `{events}`.
- Frontend : `frontend/src/types/ws-debug.ts` exporte le type `DebugEvent` mirror du backend.
- Frontend : `SphereUI.tsx` installe un `keydown` listener au mount qui détecte `event.metaKey && event.shiftKey && event.code === "KeyD"`, ignore si `event.target` est `<input>` / `<textarea>` / `contenteditable`, et invoque la commande Tauri `toggle_debug_window` via `@tauri-apps/api/core` `invoke`.

## Acceptance criteria

- [ ] Lancement `pnpm tauri dev`, fenêtre Sphere apparaît normalement, fenêtre debug n'est PAS visible.
- [ ] Presser `Cmd+Shift+D` avec focus sur Sphere ouvre la fenêtre debug (titre "Bob · Debug").
- [ ] Presser `Cmd+Shift+D` à nouveau hide la fenêtre debug (sans détruire le state).
- [ ] Presser `Cmd+Shift+D` une 3e fois ré-affiche la même fenêtre avec l'historique préservé côté frontend ET les events backend replayés depuis le ring buffer.
- [ ] Presser `D` ou `Shift+D` sans `Cmd` pendant que je tape un message dans l'InputField NE déclenche PAS l'ouverture.
- [ ] Envoyer un message texte à Bob fait apparaître une ligne `[ts] [input] User envoie: "..."` dans la fenêtre debug.
- [ ] Fermer puis rouvrir la fenêtre debug (Cmd+Shift+D off/on) replay les events précédents (jusqu'à 2000) avec leur `ts` original.
- [ ] La fenêtre debug est déplaçable indépendamment de Sphere et survit à un resize de Sphere.
- [ ] Aucune régression sur le `/ws` user-facing (chat normal, sub-tasks, TTS marchent comme avant).
- [ ] Le backend ne crash pas si aucun client n'est connecté à `/ws/debug` (le buffer continue d'accumuler).

## Blocked by

None - can start immediately
