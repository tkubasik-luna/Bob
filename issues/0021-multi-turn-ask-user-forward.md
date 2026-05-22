## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Sub-agent devient **multi-turn** : il peut maintenant émettre `ask_user(question)` en plus de `done(result)`. Quand il le fait, sa task passe en `waiting_input` et c'est Jarvis qui paraphrase la question dans le chat principal avec son ton. L'utilisateur répond à Jarvis, qui route la réponse via le tool `forward_to_subtask(task_id, response)`, le sub-agent reprend son loop avec le nouveau message dans son history.

Slice introduit aussi le `EventBus` interne (asyncio pub/sub) qui découple la production d'events sub-agent de leur consommation par le handler de proactivité Jarvis.

Prompt de paraphrase Jarvis hardcoded au shipping : `"Une de tes sous-tâches ({task_title}) a besoin d'une info : '{raw_question}'. Reformule cette question pour l'utilisateur dans ton ton, en 1-2 phrases max. Ne mentionne pas le mot 'sub-agent', dis 'la tâche'."`. Tune ultérieur possible mais pas un blocage de slice.

Flow end-to-end démontrable :
1. User : "Draft un email pour mon manager."
2. Jarvis : `spawn_subtask("Draft email manager", "...")` → spawn.
3. Sub-agent LLM : `ask_user("Quel ton : formel ou amical ?")`.
4. Task → `waiting_input`. EventBus émet event. ProactivityHandler trigger Jarvis.
5. Jarvis push message proactif chat : "Pour cet email, tu veux un ton formel ou plutôt amical ?" (paraphrase, avec son ton).
6. User répond dans chat : "Amical."
7. Jarvis LLM appelle `forward_to_subtask(task_id, "Amical.")`.
8. Sub-agent reprend, LLM call avec history mise à jour, émet `done(result)`.
9. Task → `done`.

## Acceptance criteria

- [ ] `SubAgentRunner` loop supporte `ask_user(question)` : persist, transition `waiting_input`, émet event `task_state_changed`.
- [ ] `EventBus` asyncio pub/sub : topics `task_state_changed`, `task_message_added`. Subscribers s'abonnent au boot.
- [ ] `ProactivityHandler` subscriber : sur event `ask_user` ou `done`, appelle `Orchestrator.generate_proactive_message(task_id, event_kind)`.
- [ ] `Orchestrator.generate_proactive_message` appelle Jarvis LLM avec prompt dédié (paraphrase) → push WS `assistant_msg` avec flag `proactive: true`.
- [ ] Tool `forward_to_subtask(task_id, response)` exposé à Jarvis ; ajoute message `user` dans `task_messages` et reprend le sub-agent en background.
- [ ] Sub-agent reprend depuis `waiting_input` → `running` avec history mise à jour.
- [ ] Card sidebar reflète `waiting_input` (icon ⏳ ou couleur jaune), `running` reprend après forward.
- [ ] WS `assistant_msg` étendu avec champ `proactive: bool` (default false).
- [ ] Frontend distingue messages proactifs (subtle bord/icon Jarvis "auto-push") des réponses sur prompt user.
- [ ] Tests `SubAgentRunner` : mock LLM séquence `ask_user` puis `done` après forward → transitions correctes + history sub-agent contient bien la réponse user.
- [ ] Tests `Orchestrator` : tool call `forward_to_subtask` → message inséré au bon endroit, sub-agent retombe en `running`.
- [ ] Prompt de paraphrase hardcoded dans le code (constant module-level), pas dans `jarvis.md`.

## Blocked by

- issues/0018-first-task-spawn-end-to-end.md
- issues/0019-sidebar-ui-shell-ws-events.md
