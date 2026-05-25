## Parent

prd/0005-debug-view.md

## What to build

Compléter l'instrumentation backend pour que la vue debug montre l'ensemble du flow interne de Bob, pas seulement l'input utilisateur. Ajouter la propagation `turn_id` via `ContextVar`, l'appariement `correlation_id` sur les ops longues, et un filet de sécurité structlog qui auto-forward les WARN+ non instrumentés.

Périmètre :

- Backend `debug_log.py` : ajouter un `ContextVar[str | None]` nommé `current_turn_id`. Modifier `emit_debug(...)` pour lire le ContextVar et le mettre automatiquement dans chaque event créé (None si pas dans un turn).
- Backend `debug_log.py` : ajouter un helper `start_turn() -> str` qui génère un UUID, le set dans le ContextVar et retourne l'ID. Le code appelant peut aussi explicitement set via un context manager si besoin.
- Backend `debug_log.py` : installer un structlog processor (ou un handler logging.Handler équivalent) qui intercepte chaque log record de niveau WARN ou ERROR émis par les loggers `bob.*` et appelle `emit_debug(category="system", severity="warn"|"error", source=logger_name, summary=event_msg, payload={...record_fields})`. Filet de sécurité — ne pas dupliquer les events déjà émis explicitement.
- Backend `orchestrator.py` : à l'entrée de `process_user_message`, appeler `start_turn()` AVANT le `emit_debug` user_msg existant. Ajouter `emit_debug` au début du thinking (cat=`decision`, sev=`info`, summary=`Jarvis réfléchit`), à la fin du thinking (cat=`decision`, sev=`info`, summary=`Jarvis a fini de réfléchir`), à la décision `_spawn_subtask` (cat=`decision`, sev=`info`, summary=`Jarvis lance sub-task '<title>'`, payload contient title + goal + task_id), et à l'émission `assistant_msg` proactive ou réactive (cat=`output`, sev=`info`, summary=`Bob répond: "..."`, payload contient speech + ui blocks + proactive flag).
- Backend `llm_client.py` (ou wherever `complete()` est appelé) : générer un `correlation_id = uuid4().hex` localement, `emit_debug` avant le call HTTP (cat=`llm`, sev=`info`, source=`bob.llm_client.complete`, summary=`LLM call démarré (N tokens prompt, model=X)`, payload contient `messages` array complet + model name + token_count_estimate, `correlation_id=cid`), puis `emit_debug` après réponse (cat=`llm`, sev=`info`, summary=`LLM call terminé en Xms (N tokens response)`, payload contient response complète + latency_ms + tokens_in + tokens_out, même `correlation_id`). En cas d'exception, émettre un `*_end` quand même avec `severity="error"` et le traceback dans payload.
- Backend `sub_agent_runner.py` : `emit_debug` dans `_handle_done` (cat=`task`, sev=`info`, summary=`Sub-task '<title>' terminée`, payload contient result), `_handle_progress` (cat=`task`, sev=`debug`, summary=`Sub-task '<title>' progresse: <status>`), `_handle_ask_user` (cat=`task`, sev=`info`, summary=`Sub-task '<title>' demande user input`, payload contient question), `_fail` (cat=`task`, sev=`warn`, summary=`Sub-task '<title>' a échoué: <reason>`, payload contient exception).
- Backend `task_scheduler.py` : `emit_debug` dans `_promote` pending→running (cat=`task`, sev=`info`, summary=`Sub-task '<title>' démarre`), et dans `cancel` (cat=`task`, sev=`info`, summary=`Sub-task '<title>' annulée`).
- Backend `ws_router.py` : `emit_debug` au connect (cat=`system`, sev=`info`, summary=`Client WS connecté (session=<id>)`), au disconnect (cat=`system`, sev=`info`, summary=`Client WS déconnecté`), `tts_preparing` (cat=`voice`, sev=`info`, summary=`Kokoro download...`), `tts_ready` (cat=`voice`, sev=`debug`, summary=`Kokoro prêt`), `audio_start` (cat=`voice`, sev=`debug`, summary=`Audio stream démarré (msg=<id>)`), `audio_end` (cat=`voice`, sev=`debug`, summary=`Audio stream terminé`), `audio_error` (cat=`voice`, sev=`warn`, summary=`Audio erreur: <reason>`, payload contient exception).
- Backend : vérifier que les sub-tasks (créées via `asyncio.create_task`) héritent du `ContextVar.current_turn_id` du turn parent. C'est le comportement standard de `contextvars` Python mais ajouter un commentaire et un test manuel pour confirmer.

Frontend : aucun changement UI. Les events arrivent juste plus nombreux et avec `turn_id` peuplé. Toujours pas de toolbar ni d'expand, juste le feed brut de slice 0038 qui devient plus riche.

## Acceptance criteria

- [ ] Envoi d'un message à Bob produit dans le feed debug, dans l'ordre : `input User envoie` → `decision Jarvis réfléchit` → `llm LLM call démarré` → `llm LLM call terminé` → (éventuellement `decision Jarvis lance sub-task`) → `decision Jarvis a fini de réfléchir` → `output Bob répond`.
- [ ] Tous les events d'un même turn partagent le même `turn_id` dans le payload JSON visible (vérifiable via DevTools React ou en logguant côté frontend).
- [ ] Un nouveau message user génère un nouveau `turn_id` distinct.
- [ ] Une sub-task spawnée dans le turn hérite du `turn_id` parent pour ses propres events `llm`/`task`.
- [ ] Les events `llm_call_start` et `llm_call_end` ont le même `correlation_id`.
- [ ] Le payload de `llm_call_start` contient le full `messages` array et le `model` name.
- [ ] Le payload de `llm_call_end` contient la response complète, `latency_ms`, `tokens_in`, `tokens_out`.
- [ ] Forcer une exception loggée en `logger.error()` dans n'importe quel module `bob.*` fait apparaître un event `system / error` dans le feed sans avoir à instrumenter explicitement le site.
- [ ] Activer le mode vocal et déclencher un TTS génère les events `voice` (`tts_preparing`, `tts_ready`, `audio_start`, `audio_end`) dans l'ordre.
- [ ] La connexion/déconnexion du WS user (`/ws`) génère les events `system` correspondants.
- [ ] Aucune régression sur les flows existants (chat, voice, sub-tasks).
- [ ] Le pipeline `emit_debug` ne bloque pas le caller même si aucun client `/ws/debug` n'est connecté ou si un client est lent (overflow strategy `drop_oldest` côté subscriber).

## Blocked by

issues/0038-debug-view-tracer.md
