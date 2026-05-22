## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Foundation persistent du thread Jarvis. La conversation actuelle in-memory (`ConversationStore`) est remplacée par une persistence SQLite, et la personnalité de Jarvis devient configurable via un fichier markdown éditable.

End-to-end : un utilisateur envoie un message → Jarvis répond avec sa personnalité chargée depuis `~/.bob/jarvis.md` → la conversation est persistée dans `~/.bob/bob.db` (table `jarvis_messages`) → après un redémarrage de l'app, l'historique est restauré dans le chat principal.

Inclut le runner de migration SQLite (utilisé aussi par les slices suivantes) et le `JarvisPromptLoader` qui ship un default bundled si le fichier user est absent.

## Acceptance criteria

- [ ] Fichier `~/.bob/jarvis.md` créé avec contenu default si absent au boot.
- [ ] Table `jarvis_messages` créée via migration script idempotente (ré-run safe).
- [ ] Migration runner réutilisable pour les futures tables (signature simple, ordering par filename).
- [ ] `ConversationStore` remplacé par `JarvisStore` SQLite-backed (interface : `append(role, content)`, `history() -> list`, `clear()`).
- [ ] System prompt Jarvis injecté depuis `~/.bob/jarvis.md` à chaque LLM call.
- [ ] Restart Bob : l'historique du chat principal est rechargé et visible dans le frontend au reconnect WS.
- [ ] Existing WS events (`user_msg`, `assistant_msg`, `thinking`) inchangés côté frontend.
- [ ] `BOB_DATA_DIR` env var override (default `~/.bob/`).
- [ ] Tests : `JarvisStore` CRUD + reload simulé (SQLite in-memory).

## Blocked by

None - can start immediately.
