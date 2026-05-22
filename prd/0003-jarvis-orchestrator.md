# 0003 — Jarvis Orchestrator (Multi-Task Assistant)

## Problem Statement

Bob actuel est un chatbot 1-thread : chaque message attend la réponse complète du LLM avant qu'on puisse continuer. Pas de mémoire entre restarts (conversation in-memory uniquement). Pas de notion de tâche : tout est confondu dans un seul fil de discussion qui mélange demandes longues (recherche, draft) et échanges courts.

L'utilisateur veut un **assistant personnel type "Jarvis"** : un LLM unique avec personnalité, seul interlocuteur, qui sait **déléguer en arrière-plan** des sous-tâches longues à d'autres LLMs sans bloquer la conversation principale. L'utilisateur ne parle jamais directement aux sous-agents : Jarvis est le chef d'équipe, relaie les questions, synthétise les résultats.

## Solution

Refactor du chat actuel en architecture orchestrée :

- **Jarvis** : LLM principal unique avec personnalité customisable (system prompt depuis `~/.bob/jarvis.md`). Seul interlocuteur de l'utilisateur (input texte en MVP, voix push-to-talk en phase 2 sur l'infra TTS existante). Persistance SQLite : la conversation principale survit au restart.
- **Sous-tâches** : threads LLM isolés exécutés par des sub-agents en background. Chaque sous-tâche a un goal explicite, un titre, son own history. Multi-turn : un sub-agent peut émettre `done(result)`, `ask_user(question)`, ou `progress(status)`.
- **Orchestration par tool calling** : Jarvis tranche à chaque message utilisateur via tool calls. Tools disponibles : `spawn_subtask(title, goal)`, `forward_to_subtask(task_id, response)`, `cancel_subtask(task_id)`. Si aucun tool n'est appelé, Jarvis répond directement dans le chat principal.
- **Proactivité** : quand un sub-agent émet `done` ou `ask_user`, le backend trigger un appel Jarvis qui push un message dans le chat principal. L'utilisateur perçoit Jarvis comme "vivant" et au courant en temps réel.
- **UI multi-pane** : chat Jarvis au centre, sidebar droite avec cards des sous-tâches. Chaque card affiche titre, état (icon/couleur), badge `needs_attention` si nouveau. Click sur card = drawer slide-in avec transcript raw du sub-agent + résultat complet.
- **Concurrency** : cap dur à 3 sous-tâches `running` simultanées, surplus en `pending`. Cancellation possible via × sur card ou via demande à Jarvis.
- **Backends LLM séparés** : config `JARVIS_BACKEND` et `SUBAGENT_BACKEND` indépendants (Claude CLI ou LM Studio). Permet ex. Jarvis=Claude (qualité conversation) + sub-agents=LM Studio (cost).

## User Stories

1. As an utilisateur, I want que ma conversation avec Jarvis survive au restart de Bob, so that je puisse reprendre une discussion entamée la veille sans perdre le contexte.

2. As an utilisateur, I want que Jarvis ait une personnalité cohérente définie par moi via un fichier markdown éditable, so that je puisse calibrer son ton (sérieux/joueur/concis).

3. As an utilisateur, I want demander une tâche longue (ex. "draft 5 versions d'un email") à Jarvis, so that il puisse la lancer en background pendant que je continue à converser sur autre chose.

4. As an utilisateur, I want voir une sidebar à droite avec toutes mes sous-tâches actives, so that je puisse à tout moment savoir ce qui tourne ou attend mon input.

5. As an utilisateur, I want que chaque sous-tâche dans la sidebar affiche son état (pending/running/waiting_input/done/failed) avec un indicateur visuel clair, so that je comprenne d'un coup d'œil où elles en sont.

6. As an utilisateur, I want que Jarvis m'annonce proactivement dans le chat principal quand une sous-tâche est terminée ou attend mon input, so that je n'aie pas à monitorer la sidebar en permanence.

7. As an utilisateur, I want que les questions des sous-agents soient paraphrasées et relayées par Jarvis avec son ton, so that je ne casse pas la métaphore "Jarvis est mon seul interlocuteur".

8. As an utilisateur, I want répondre à Jarvis en langage naturel quand il me transmet une question d'un sub-agent, so that Jarvis route lui-même ma réponse vers le bon sub-agent sans que j'aie à clicker quoi que ce soit.

9. As an utilisateur, I want cliquer une card sous-tâche dans la sidebar pour ouvrir un drawer avec son transcript interne raw + résultat brut, so that je puisse debug/vérifier ce que le sub-agent a réellement produit.

10. As an utilisateur, I want que les sous-tâches `done`/`failed` restent affichées dans la sidebar avec un badge ✓ ou ⚠ jusqu'à ce que je les dismisse manuellement (×), so that je ne rate pas une complétion si j'étais absent.

11. As an utilisateur, I want pouvoir annuler une sous-tâche en cours via un bouton × sur la card sidebar, so that je puisse stopper rapidement une tâche qui dérive.

12. As an utilisateur, I want pouvoir aussi annuler une sous-tâche en disant à Jarvis "annule la tâche email", so that je puisse rester dans le flot conversationnel sans switcher sur la souris.

13. As an utilisateur, I want que Bob plafonne à 3 sous-tâches `running` simultanément et queue les autres en `pending`, so que mon LLM local ne soit pas saturé et que les coûts Claude restent maîtrisés.

14. As an utilisateur, I want voir quelles sous-tâches sont en `pending` (en attente d'un slot d'exécution), so que je sache que ça va démarrer bientôt sans paniquer.

15. As an utilisateur, I want que Jarvis synthétise le résultat d'une sous-tâche done dans le chat principal en quelques lignes, so que je n'aie pas à ouvrir le drawer pour avoir une idée du résultat.

16. As an utilisateur, I want configurer indépendamment le backend LLM de Jarvis (`JARVIS_BACKEND`) et celui des sub-agents (`SUBAGENT_BACKEND`), so que je puisse mettre Jarvis sur Claude (qualité) et sub-agents sur LM Studio (cost/privacy).

17. As an utilisateur, I want que le voice mode existant (TTS Kokoro) s'intègre dans l'état `speaking` de Jarvis, so que la voix continue de fonctionner sans rupture quand j'active le toggle.

18. As an utilisateur, I want voir un état `thinking` visible quand Jarvis réfléchit (LLM call en cours), so que je sache qu'il bosse et que je ne renvoie pas un message dans le vide.

19. As an utilisateur, I want que la sidebar reste cohérente même si plusieurs events sub-agent arrivent presque en même temps, so que je ne perde pas d'info ni voie d'ordre inversé.

20. As an utilisateur, I want que Bob redémarre proprement avec mes sous-tâches `running`/`waiting_input`/`pending` restaurées depuis SQLite, so que je puisse poursuivre une session multi-jours sans perte.

21. As an utilisateur, I want que Jarvis voit dans son contexte un résumé de l'état des sous-tâches actives (titre, état, dernière update), so qu'il puisse en parler intelligemment quand je l'interroge ("où en est la recherche ?").

22. As an utilisateur, I want que les sub-agents soient des LLM purs sans tools (web/file/bash) en MVP, so que les capacités soient symétriques entre Claude et LM Studio et que je puisse rester full-local si je veux.

23. As an utilisateur, I want que Jarvis ne délègue à un sub-agent QUE quand il juge la tâche longue/autonome (via tool call `spawn_subtask`), so que les questions simples ("quelle heure ?") restent en réponse directe sans overhead.

24. As an utilisateur, I want en phase 2 pouvoir trigger Jarvis via push-to-talk (bouton hold ou raccourci global), so que je puisse parler hands-busy sans wake word always-on.

25. As an utilisateur, I want que les sous-tâches `cancelled` apparaissent comme une variante de `failed` avec raison "user_cancelled" dans le drawer, so que je garde une trace de mes décisions.

26. As an utilisateur, I want que le sub-agent puisse émettre `progress(status)` pour update son état affichable sans terminer, so que je voie l'avancement d'une tâche longue (ex: "j'analyse le 3e document sur 10").

27. As an développeur, I want que `TaskStore` expose une interface CRUD simple (create/get/list/append_message/update_state) sur SQLite, so que je puisse swap la couche persistence sans toucher au reste.

28. As an développeur, I want que `SubAgentRunner` soit testable en isolation avec un LLM mock, so que je valide le multi-turn loop + parsing actions sans dépendre d'un LLM réel.

29. As an développeur, I want que `TaskScheduler` encapsule toute la logique cap/queue/promote-pending, so que la concurrence soit centralisée et testable.

30. As an développeur, I want un `EventBus` interne backend avec topics (`task_state_changed`, `task_message`), so que Jarvis-trigger et WS-push soient des subscribers découplés.

31. As an développeur, I want que `LLMClient` expose une abstraction tool-calling unifiée (Claude + LM Studio), so que `Orchestrator` n'ait pas à connaître le backend sous-jacent.

## Implementation Decisions

### Domain model

- **Task** = thread isolé. Champs : `id`, `title`, `goal`, `state` (`pending`/`running`/`waiting_input`/`done`/`failed`), `needs_attention` (bool), `result` (nullable), `created_at`, `updated_at`, `parent_task_id` (toujours NULL en MVP — pas de sub-sub-tasks).
- **TaskMessage** = entrée dans l'history d'une task. Champs : `id`, `task_id`, `role` (`system`/`user`/`assistant`/`tool`), `content`, `action` (nullable enum : `done`/`ask_user`/`progress`), `created_at`.
- **JarvisMessage** = entrée dans l'history du thread Jarvis principal. Champs identiques à TaskMessage mais sans `task_id` (thread singleton).
- Tables SQLite : `jarvis_messages`, `tasks`, `task_messages`. Migration script à la racine `backend/src/bob/db/migrations/`.

### Orchestrator

- `Orchestrator.process_user_message(text)` : appel LLM Jarvis avec contexte (history Jarvis + résumé tasks actives + tools définis). Output = soit tool calls, soit texte direct.
- Tools exposés à Jarvis :
  - `spawn_subtask(title: str, goal: str) -> task_id` : crée task `pending`, enqueue.
  - `forward_to_subtask(task_id: str, response: str) -> None` : ajoute message user dans task_messages, reprend le run sub-agent.
  - `cancel_subtask(task_id: str, reason?: str) -> None` : marque task `failed` avec reason="user_cancelled".
- Si aucun tool, output texte = `assistant_msg` poussé via WS.

### SubAgentRunner

- Loop pour une task : load history → call LLM avec system prompt template (`"You are a sub-agent. Your goal: {goal}. Emit ONE of: done(result), ask_user(question), progress(status)."`) → parse output JSON structuré → dispatch :
  - `done` : persiste result, transition `running` → `done`, émet event `task_state_changed`.
  - `ask_user` : persiste question, transition `running` → `waiting_input`, émet event.
  - `progress` : persiste status, reste en `running`, émet event, ré-itère immédiatement (max N=10 iterations consécutives sans done/ask_user pour éviter loop infini).
- Parsing : structured output (JSON mode pour LM Studio si supporté, sinon prompt + regex fallback). Claude CLI : utilise tool calling natif.

### TaskScheduler

- Maintient compteur in-memory `running_count`.
- API : `enqueue(task_id)`, `on_task_done(task_id)`, `promote_next_pending()`.
- Quand `running_count < MAX_RUNNING_TASKS` (default 3) ET au moins une task `pending` : promote la plus ancienne `pending` → `running`, lance `SubAgentRunner` en background asyncio task.
- Au boot : scan SQLite, re-promote les `running`/`pending` cohérents avec cap.

### EventBus

- Pub/sub asyncio simple. Topics : `task_state_changed`, `task_message_added`.
- Subscribers MVP :
  - `JarvisProactivityHandler` : sur `task_state_changed` ∈ {done, waiting_input, failed} OU `task_message_added` avec `action=ask_user`, déclenche `Orchestrator.generate_proactive_message(task_id, event_kind)`.
  - `WsBroadcaster` : pousse `task_*` events au frontend de la session connectée.

### Proactive messages

- `Orchestrator.generate_proactive_message(task_id, event_kind)` appelle Jarvis LLM avec un prompt spécial : "Une sous-tâche vient d'émettre {event_kind}. Goal: {goal}, Result/Question: {payload}. Annonce-le à l'utilisateur dans ton ton." Output texte → WS event `assistant_msg` avec flag `proactive: true`.
- Queue interne pour éviter race conditions : si Jarvis est en `thinking` ou `speaking`, les events sont bufferisés et flushés à `idle`.

### JarvisPromptLoader

- Au boot, lit `~/.bob/jarvis.md`. Si absent, écrit un default bundled (ex. inspiration "tu es Jarvis, AI personnel, ton calme et concis...").
- Chargé une fois par session, pas de reload runtime en MVP.

### LLMClient abstraction

- Interface commune : `complete(messages, tools?) -> Either[Tools, Text]`.
- `LMStudioClient` : utilise OpenAI-compatible API. Tool calling supporté si modèle compatible (à documenter). Fallback : structured prompt + JSON parsing.
- `ClaudeCliClient` (extend existing) : map vers tool calling natif Claude.
- Sub-agent peut utiliser le même client (sans tools, juste structured output JSON pour les 3 actions).

### Config

- `JARVIS_BACKEND` : `claude_cli` | `lm_studio` (default identique à existant `LLM_PROVIDER`).
- `SUBAGENT_BACKEND` : `claude_cli` | `lm_studio` (default = même que Jarvis).
- `MAX_RUNNING_TASKS` : int (default 3).
- `BOB_DATA_DIR` : path (default `~/.bob/`), contient `bob.db` + `jarvis.md`.

### WS event contract (nouvelles variants)

- Server → Client :
  - `task_created` : `{task_id, title, goal, state, created_at}`.
  - `task_updated` : `{task_id, state, needs_attention, updated_at}`.
  - `task_message` : `{task_id, role, content, action?, created_at}` (pour drawer transcript live).
  - `task_result` : `{task_id, result}` (quand done).
  - `task_cancelled` : `{task_id, reason}`.
  - `assistant_msg` : champ ajouté `proactive: bool`.
- Client → Server :
  - `cancel_task` : `{task_id}`.
  - `dismiss_task` : `{task_id}` (cache la card sidebar, ne supprime pas la row SQLite).
  - `user_msg` : inchangé. C'est Jarvis qui décide du routing via tools.

### Frontend layout

- `ChatView` split flex : chat principal (~70%) + `TaskSidebar` (~30%).
- `TaskSidebar` : vertical list of `TaskCard`. Sticky header "Tâches en cours".
- `TaskCard` : icon état, titre, hover ×. Click sur card (hors ×) → ouvre `TaskDrawer`.
- `TaskDrawer` : slide-in depuis la droite, full-height, montre goal + transcript task_messages + result si done.
- Zustand slice `tasks` : map `task_id` → task object. Mise à jour sur chaque WS event.

### Phase 2 (voice trigger) — non implémenté en MVP mais préparé

- STT engine : faster-whisper local (model small/base).
- Trigger : push-to-talk via bouton + shortcut clavier global (Tauri).
- Audio capture frontend → WS binary frame → STT backend → texte → `user_msg`.
- État `listening` de Jarvis activé pendant capture.

## Testing Decisions

### Principes

- Tester le comportement externe (interfaces publiques), pas l'implémentation interne. Mock les dépendances volatiles (LLM, WS).
- Privilégier des tests unitaires sur les modules logiques purs ; éviter les tests d'intégration full-stack en MVP.
- Prior art : `backend/tests/test_text_normalizer.py`, `backend/tests/test_ws_chat.py` (pytest async).

### Modules testés

1. **TaskStore** — `backend/tests/test_task_store.py`. Coverage :
   - Create task → list_tasks contient l'id.
   - State transitions valides (`pending` → `running` → `done`).
   - Append messages → get_task_messages retourne dans l'ordre.
   - Restart simulé (in-memory SQLite reload) → state préservé.
2. **TaskScheduler** — `backend/tests/test_task_scheduler.py`. Coverage :
   - Enqueue 5 tasks avec cap=3 → 3 `running`, 2 `pending`.
   - `on_task_done` promote la plus ancienne `pending`.
   - Cap respecté sous bursts concurrents.
3. **SubAgentRunner** — `backend/tests/test_sub_agent_runner.py`. Coverage :
   - LLM mock retourne `done(result)` → task transition correct + result persisté.
   - LLM mock retourne `ask_user(q)` → transition `waiting_input` + event émis.
   - LLM mock enchaîne `progress` ×3 puis `done` → boucle correcte, max-iterations cap respecté.
   - LLM mock retourne format JSON invalide → fallback retry/error.
4. **Orchestrator** — `backend/tests/test_orchestrator.py`. Coverage :
   - LLM mock retourne tool call `spawn_subtask` → task créée dans store.
   - LLM mock retourne `forward_to_subtask` → message ajouté à la bonne task.
   - LLM mock retourne `cancel_subtask` → task marquée failed avec reason.
   - LLM mock retourne texte direct → `assistant_msg` push WS, aucun side effect tasks.

### Non testé en MVP

- WS plumbing (déjà testé via `test_ws_chat.py` pour le pattern, étendu progressivement).
- React components (sidebar, drawer, card) — manual smoke test.
- E2E full-flow (frontend → backend → LLM réel).
- `EventBus` — assez simple pour ne pas mériter ses propres tests, validé via les tests downstream.
- `JarvisPromptLoader` — trivial file read.

## Out of Scope

- **Sub-agent tools** (web fetch, file read, bash). MVP = LLM pur. Phase ultérieure.
- **Sub-sub-tasks** (un sub-agent qui spawn lui-même). Hiérarchie 1 niveau seulement.
- **Voice input (STT)**. Phase 2 séparée. Infra TTS existante reste branchée sur Jarvis.
- **Wake word** ("Hey Bob"). Pas même en phase 2. Push-to-talk only.
- **Per-task backend choice à l'UI**. Backend choisi globalement via config.
- **Auto-archive done tasks**. Reste visible jusqu'à dismiss manuel.
- **Multi-user / multi-device sync**. Bob = desktop solo, 1 user.
- **Reprise d'une task `done` ou `failed`**. Une fois terminée, lecture seule. Re-spawn manuel via Jarvis si user le demande.
- **Typed task kinds** (email / search / draft). Free-form en MVP.
- **UI settings panel** pour éditer la personnalité Jarvis. Édition du `.md` à la main.
- **Cleanup auto** des sub-task transcripts. SQLite grandit librement, vacuum manuel si besoin.
- **Streaming partial LLM output** pour Jarvis (response progressive). Pas en MVP, message complet en une fois.

## Further Notes

- **Phasing implémentation** (séquence shippable) :
  - **P1** — Refactor Jarvis (jarvis.md + SQLite `jarvis_messages` + thread unique persistent). Pas de tasks.
  - **P2** — Tables `tasks` + `task_messages`, tool `spawn_subtask`, `SubAgentRunner` 1-shot (juste `done`).
  - **P3** — Sidebar UI : cards minimales avec état.
  - **P4** — Multi-turn sub-agent : `ask_user` + `forward_to_subtask`. Proactivité Jarvis (push).
  - **P5** — Concurrency cap + queue + `cancel_subtask` (sidebar × + tool) + drawer transcript + dismiss done.
  - **P6 (phase voice)** — STT push-to-talk + état `listening`.

- **Risques techniques** :
  - Tool calling support LM Studio variable selon le modèle. Fallback structured JSON parsing nécessaire et bien testé.
  - Race condition entre proactive push Jarvis et user qui tape un message en même temps. Buffer + flush à `idle` requis.
  - Context window Jarvis grandit indéfiniment (1 thread permanent). Truncation FIFO simple en MVP, compression/summarization en phase 2.

- **Glossaire** :
  - **Jarvis** = LLM principal singleton, personnalité, interlocuteur user.
  - **Sub-agent** = LLM exécutant une task isolée.
  - **Task** = sous-tâche déléguée, thread isolé avec own history, persisté.
  - **Action** = output structuré d'un sub-agent (`done`/`ask_user`/`progress`).
  - **Proactive message** = message Jarvis poussé sans request user (sur event sub-agent).

- **Dépendances avec features existantes** :
  - Voice mode 0002 (TTS Kokoro) reste branché. `speaking` state Jarvis pilote l'audio playback (déjà câblé).
  - Server-driven UI components (`ui_registry`, `Dispatcher`) — non touchés. Future `task_result` pourrait utiliser ces components pour rendu structuré.
