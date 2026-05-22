## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Deep module `TaskStore` qui encapsule toute la persistence des sous-tâches sur SQLite. Pas d'UI, pas de WS events à ce stade : juste une couche data isolée et testable, prête à être consommée par l'orchestrateur et le scheduler.

Schémas tables `tasks` et `task_messages` créés via le migration runner du slice #1. Interface CRUD couvrant : création de task, lecture, transitions d'état (avec validation), append/lecture des messages internes, listing par état.

À la fin du slice, un script de smoke démo peut créer une task, ajouter quelques messages, faire une transition, recharger une fresh instance, et tout est cohérent.

## Acceptance criteria

- [ ] Migration ajoute tables `tasks` (id, title, goal, state, needs_attention, result, created_at, updated_at, parent_task_id NULLABLE) et `task_messages` (id, task_id FK, role, content, action NULLABLE, created_at).
- [ ] `TaskStore.create_task(title, goal) -> task_id` insère avec state=`pending`.
- [ ] `TaskStore.update_state(task_id, new_state, reason?)` valide la transition (`pending`→`running`, `running`→`waiting_input`/`done`/`failed`, `waiting_input`→`running`).
- [ ] `TaskStore.append_message(task_id, role, content, action?)` persiste dans `task_messages`.
- [ ] `TaskStore.get_task(task_id) -> Task` + `list_tasks(state?, limit?) -> list[Task]`.
- [ ] `TaskStore.get_task_messages(task_id) -> list[TaskMessage]` ordre chronologique.
- [ ] `TaskStore.set_needs_attention(task_id, bool)`.
- [ ] Tests : transitions valides + invalides (raise ValueError), reload SQLite préserve state, append ordre stable.
- [ ] Prior art tests : `backend/tests/test_text_normalizer.py` pattern.

## Blocked by

- issues/0015-jarvis-foundation-sqlite-prompt.md
