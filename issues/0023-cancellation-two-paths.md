## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Cancellation des sous-tâches en cours, deux chemins :

1. **Sidebar** : bouton × au hover sur la card → envoie WS event `cancel_task(task_id)` → backend cancel.
2. **Jarvis** : utilisateur dit "annule la tâche email" → Jarvis appelle tool `cancel_subtask(task_id, reason?)`.

État final = variante de `failed` avec `reason="user_cancelled"` (ou la reason fournie par Jarvis si user a précisé). Le scheduler libère le slot, promote une `pending` si présente.

L'asyncio task du sub-agent doit être réellement interrompue (asyncio cancellation) — pas juste un flag ignoré. Si le sub-agent est en plein milieu d'un LLM call HTTP, on coupe net.

## Acceptance criteria

- [ ] Tool `cancel_subtask(task_id, reason?)` exposé à Jarvis ; appelle `TaskScheduler.cancel(task_id, reason)`.
- [ ] `TaskScheduler.cancel(task_id, reason="user_cancelled")` : annule l'asyncio task du sub-agent (`task.cancel()` + await), transition state → `failed` avec reason persistée dans `tasks.result` (ou champ dédié `failure_reason`), libère slot, promote pending.
- [ ] WS client-to-server event `cancel_task` `{task_id}` ; backend route vers `TaskScheduler.cancel(task_id, reason="user_cancelled")`.
- [ ] `TaskCard` sidebar : bouton × visible au hover (top-right), click → dispatch WS event + désactive card (loading state).
- [ ] Cancellation d'une task déjà `done`/`failed` : no-op silencieux (pas d'erreur).
- [ ] Cancellation d'une task `pending` (pas encore démarrée) : transition directe à `failed`, libère du slot virtuel (pas de slot occupé).
- [ ] Card cancellled affiche reason au hover (tooltip "Annulée par l'utilisateur" ou raison custom Jarvis).
- [ ] Tests `TaskScheduler` : cancel d'une running → asyncio task cancelled + slot libéré + pending promu ; cancel d'une pending → state failed direct.
- [ ] Tests `Orchestrator` : tool `cancel_subtask` route bien vers scheduler.

## Blocked by

- issues/0019-sidebar-ui-shell-ws-events.md
- issues/0021-multi-turn-ask-user-forward.md
