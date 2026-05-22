## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Premier slice end-to-end qui prouve que Jarvis peut **déléguer une sous-tâche** à un sub-agent et obtenir un résultat. UI sidebar pas encore livrée — la vérification se fait via la DB et les logs.

Flow attendu : utilisateur dit à Jarvis "Draft 3 versions d'un email de remerciement". Jarvis (LLM call avec tools) émet un `spawn_subtask(title, goal)`. L'orchestrateur crée la task dans `TaskStore` (state `pending`), promote en `running`, lance le `SubAgentRunner` en background. Sub-agent appelle son LLM (sub-agent backend) avec un prompt template incluant le goal, parse output structuré, émet une action `done(result)`. Task transition à `done` avec result persisté. Jarvis répond une simple confirmation texte ("ok, je m'en occupe, je te dis dès que c'est prêt").

Pas de proactivité, pas de notification de fin pour l'instant : on valide juste que le pipeline tourne.

Slice introduit :

- `Orchestrator` (refactor `chat_service`) : appel Jarvis LLM avec contexte + tool definition `spawn_subtask`.
- `SubAgentRunner` 1-shot : load goal → call LLM → parse `done` action → persist.
- Config vars `JARVIS_BACKEND` + `SUBAGENT_BACKEND` + `MAX_RUNNING_TASKS` (utilisé en hardcode à 3 pour cap implicite ici, vrai cap arrive slice #6).

## Acceptance criteria

- [ ] `Orchestrator.process_user_message(text)` appelle Jarvis LLM avec tools=[spawn_subtask].
- [ ] Si tool call : task créée dans `TaskStore`, sub-agent lancé en background (asyncio task).
- [ ] Si pas de tool call : réponse texte directe via WS `assistant_msg` (chat normal).
- [ ] `SubAgentRunner.run(task_id)` : load goal, call LLM, parse `done(result)`, persist result + transition.
- [ ] Sub-agent émet uniquement `done` à ce stade ; autres actions = parse error logué.
- [ ] Backends séparés : `JARVIS_BACKEND` et `SUBAGENT_BACKEND` env vars, peuvent être différents.
- [ ] Test end-to-end (backend only) : mock LLM Jarvis pour qu'il émette `spawn_subtask`, mock LLM sub-agent pour `done(result)`, vérifier task DB = `done` + result persisté.
- [ ] Tests `Orchestrator` : LLM mock → spawn (task créée) ; LLM mock → texte (pas de task créée).
- [ ] Tests `SubAgentRunner` : LLM mock `done` → state + result OK ; LLM mock retourne format invalide → state=failed + error logué.

## Blocked by

- issues/0015-jarvis-foundation-sqlite-prompt.md
- issues/0016-task-store.md
- issues/0017-llm-tool-calling-abstraction.md
