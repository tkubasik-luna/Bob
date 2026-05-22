## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Jarvis devient pleinement proactif sur les events `done` : quand une sous-tâche termine, le `ProactivityHandler` trigger Jarvis qui **synthétise** le résultat dans le chat principal en quelques lignes ("La recherche est finie. En résumé : 3 papiers pertinents, le plus important est X. Tu veux que je creuse ?"). L'utilisateur reçoit l'info sans avoir à ouvrir le drawer.

Le slice règle aussi la **race condition** entre push proactif et activité utilisateur :

- Si Jarvis est `thinking` ou `speaking` : event bufferisé dans une queue interne.
- Si l'utilisateur est en train de typer (frontend signale `typing=true` via WS heartbeat) : event différé jusqu'à submit ou X secondes d'inactivité.
- Quand Jarvis devient `idle`, queue flushée FIFO.

Prompt de synthèse hardcoded : `"La sous-tâche '{task_title}' vient de terminer. Résultat brut : '{result}'. Annonce-le à l'utilisateur dans ton ton, en 2-3 lignes max. Résume les points clés, propose une suite si pertinent."`. Constant module-level, tune ultérieur possible.

À la fin du slice : Jarvis est un assistant qui "vit" — il annonce spontanément la fin d'une recherche, te demande des précisions au bon moment, synthétise ce qu'il a appris. Cohérent avec la vision "Jarvis chef d'équipe".

## Acceptance criteria

- [ ] `ProactivityHandler` étendu : sur event `task_state_changed` avec state=`done`, appelle `Orchestrator.generate_done_synthesis(task_id)`.
- [ ] `Orchestrator.generate_done_synthesis` : prompt Jarvis dédié "synthétise le résultat de cette task pour l'utilisateur en 2-3 lignes max" ; output → `assistant_msg` proactive push.
- [ ] Queue interne `Orchestrator._proactive_queue` (asyncio Queue) : bufferise events si Jarvis state ∈ {thinking, speaking}.
- [ ] Frontend signal `typing=true` via WS event `client_typing` (debounced 500ms). Backend met les events proactifs en attente tant que `typing=true` + 2s de grace.
- [ ] Quand Jarvis devient `idle` et plus de typing user, queue flushée FIFO (un message par event).
- [ ] `assistant_msg` proactive identifié visuellement côté frontend (subtle bord ou icon "auto-push" distinct des réponses normales).
- [ ] Tests `Orchestrator.generate_done_synthesis` : mock LLM Jarvis → output texte court ; vérifier WS push avec `proactive=true`.
- [ ] Tests race condition : event arrivé pendant Jarvis `thinking` → bufferisé ; après transition `idle` → flushé.
- [ ] Tests typing buffer : `client_typing=true` arrivé → events différés ; reset après inactivité 2s.
- [ ] Prompt de synthèse hardcoded en constante module-level (pas dans `jarvis.md`).

## Blocked by

- issues/0021-multi-turn-ask-user-forward.md
