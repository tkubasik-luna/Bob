# Audit perf & fiabilité — 2026-06-09

> Objectif : Bob est **oral-first**. La latence conversationnelle (fin de parole utilisateur → premier son de Bob) est la métrique reine. Fiabilité = pas de turn perdu, pas d'overlay vide, pas de stall silencieux.
>
> Doc rempli au fur et à mesure de l'audit. Chaque finding : sévérité (🔴 bloquant latence/fiabilité, 🟠 important, 🟡 amélioration), localisation, impact, piste de fix.

## Statut de l'audit

- [x] Pipeline voix (STT → turn → TTS) : time-to-first-audio
- [x] Chemin LLM (client, router, thinker, draft) : appels bloquants, prompts
- [x] Sub-agent runner + tools : séquencement, timeouts, retries
- [x] WS router + frontend : event flow, re-renders
- [x] Fiabilité transverse : erreurs, stalls, startup

---

## 1. Pipeline voix

**Budget de latence évitable identifié : ~600–800 ms par turn** (hors coût LLM incompressible).

### 🔴 Bloquants

- **1.1 — STT finalize bloque la pompe de frames à l'endpoint** — `voice_loop.py:643` + `voice_turn.py:227`. Au endpoint, `await turn.finalize()` exécute la passe whisper.cpp full-buffer (via `asyncio.to_thread`) **avant** de lancer le say-path ; le commit gate s'exécute ensuite, séquentiellement. Impact : **500 ms–2 s** selon la longueur de l'énoncé. _Fix : lancer finalize en parallèle du polling du signal sémantique d'endpoint ; ne pas bloquer le spawn du say-path dessus._

- **1.2 — Grace windows Thinker/Draft sérialisent l'endpoint** — `voice_loop.py:620–636` + `thinker_loop.py:252–263` + `speculative_draft.py:312–338`. À l'endpoint, `on_thinker_stop()` puis `on_draft_stop()` sont appelés **séquentiellement**, chacun avec `wait_for(shield(task), timeout=2.0)` (`THINKER_CANCEL_GRACE_MS=2000`). Impact : **jusqu'à 2 s** quand une passe est en vol (cas fréquent en fin de phrase). _Fix : cancel concurrent des deux loops + grace cap à 250–500 ms, voire fire-and-forget (le say-path démarre immédiatement)._

- **1.3 — Barge-in : grace windows sur le chemin d'interruption** — `voice_loop.py:402–406`. Chaîne séquentielle à l'interruption : confirm (200 ms) + cancel say-path + freeze Thinker/Draft avec leurs grace windows. Pire cas **~2.2 s** pour couper Bob ; la cible Annexe F est <300 ms. _Fix : pas de grace sur barge-in — hard `task.cancel()` immédiat ; grace windows réservées à l'endpoint._

### 🟠 Importants

- **1.4 — STT par frame via thread pool, variance élevée** — `voice_loop.py:396` + `voice_turn.py:208`. Chaque frame de 30 ms → `asyncio.to_thread(session.accept_frame)` → re-transcription incrémentale whisper.cpp. Variance 50–200 ms/frame (contention pool + coût variable) ; partials en retard → Thinker/Draft en retard. _Fix : batcher 2–3 frames par appel ; mesurer P95/P99 de `accept_frame` ; si >100 ms, revoir fenêtre/modèle._

- **1.5 — Commit gate bloque le lancement du say-path** — `voice_loop.py:655–660`. Le gate (décision draft) tourne avant le `create_task` du say-path. _Fix : spawn say-path et gate en parallèle ; le gate ne bloque que l'adoption du texte du draft, pas l'appel orchestrateur._

- **1.6 — TTS phrase par phrase, pas pipeliné** — `ws_router.py:1598–1600` + `1654–1758`. La phrase N+1 n'entre en synthèse qu'après la fin complète de la synthèse de la phrase N. ~250 ms de gap par phrase. _Fix : queue la phrase suivante à Kokoro pendant que la précédente streame encore._

- **1.7 — Debounce Thinker/Draft 250 ms sur le chemin endpoint** — `thinker_loop.py:225–234` + `speculative_draft.py:303–310`. `THINKER_DEBOUNCE_MS=250` retarde aussi le signal `user_turn_complete`. _Fix : ne pas debouncer le bit endpoint (le faire passer immédiatement) ; réduire le debounce des passes à ~100 ms._

- **1.8 — Cold starts non préchauffés au boot** — `tts_service.py:124–142` (Kokoro lazy : 500 ms–2 s au premier turn), `voice_turn.py:160–167` + `stt_engine.py:414–442` (download/load whisper au premier `voice_start`, mic non armé pendant ce temps), clients Thinker/Draft non pré-chargés (500 ms–1 s au premier partial). _Fix : warmup STT+TTS+role-clients dans le lifespan FastAPI ; armer le mic immédiatement et précharger en fond._

### 🟡 Améliorations

- **1.9 — Drafts jetés = CPU gaspillé** — `speculative_draft.py:359–392`. Draft non commité = 50–200 ms d'inférence mini-modèle perdue. _Fix : monitorer le `draft_hit` ratio (cible >60 %) ; abort préemptif du draft si intent-tool détecté._
- **1.10 — VAD/endpoint hardcodés, non tunés** — `config.py:352–353`. `VAD_PAUSE_MS=300`, `ENDPOINT_SILENCE_MS=600` : jusqu'à 600 ms de latence d'endpoint, et faux positifs sur pauses mi-phrase (fréquent en français). _Fix : métriques de distribution des pauses réelles, puis seuils adaptatifs ; le signal sémantique doit dominer le plancher de silence._
- **1.11 — Backchannel TTS await dans la boucle** — `voice_loop.py:897–958`. Synthèse backchannel awaité dans `_maybe_backchannel()`. _Fix : fire-and-forget, erreurs étouffées._
- **1.12 — Retry validation orchestrateur sur le chemin voix** — `orchestrator.py` (boucle validation). 100–300 ms par retry. _Fix : sur le chemin voix, dégrader en réponse courte hardcodée plutôt que retry._

## 2. Chemin LLM

**Dominantes : overhead subprocess Claude CLI, multiplicateur retries de validation (×2–3), reconstruction de prompt sans réutilisation de KV-cache.**

### 🔴 Bloquants

- **2.1 — Claude CLI : un process spawné par appel LLM** — `llm_client.py:1424–1434`. Chaque appel lance `claude -p` (`create_subprocess_exec`) : startup + load binaire ≈ 200–500 ms **avant** toute inférence. Avec retries : 400–1500 ms d'overhead pur par turn. En plus, **pas de streaming natif** (`llm_client.py:309–310` : fake stream synthétisé après réponse complète) → TTFT = inférence entière. _Fix : wrapper daemon persistant si faisable ; sinon assumer Claude CLI = chemin lent et privilégier LM Studio pour la voix (documenter)._ 

- **2.2 — Boucle validation/retry = ×2–3 appels LLM complets par turn au pire** — `orchestrator.py:630–720`. Retry séquentiel sur tool-call invalide ; budget 2–3 retries par outil (`validation/policy.py`). Aggravé par Claude CLI (Hermes parse tolérant → plus de malformed → plus de retries, `llm_client.py:1619–1630`). _Fix : fast-path de validation inline sur la 1re tentative ; sur chemin voix, dégrader plutôt que retry (cf. 1.12) ; mesurer le taux de retry réel._

- **2.3 — Prompt reconstruit from scratch à chaque turn, zéro réutilisation de préfixe KV-cache** — `orchestrator.py:550–800` (ContextAssembler). System prompt 2000–4000 tokens recomposé à chaque appel ; si des fragments variables (contexte temporel) arrivent tôt dans le prompt, le prefix-cache LM Studio est cassé à chaque turn → re-prefill complet (500–1000 ms sur modèle local). _Fix : ordonner les fragments stable-d'abord (system fixe → tools → variable en queue) ; vérifier le prefix-caching LM Studio ; profiler l'assembly par provider (50–200 ms estimés, DB reads inclus)._

### 🟠 Importants

- **2.4 — Trois rôles LLM (Jarvis/Thinker/Draft) sur prompts non alignés** — `thinker_loop.py:342`, `speculative_draft.py:~330`, `orchestrator.py:756`. Aucun préfixe commun entre les trois → aucune réutilisation KV croisée ; Thinker peut allonger le chemin critique avant Jarvis. _Fix : mesurer le chemin critique réel (Thinker TTFT vs Jarvis startup) ; partager le préfixe commun si possible._

- **2.5 — Swap de modèle bloque les nouveaux turns sous lock** — `llm_swap.py:173, 214–220`. Load 10–60 s sous `asyncio.Lock` ; un message utilisateur arrivant pendant un swap attend la fin. _Fix : servir les turns sur l'ancien client pendant le load (multi-load v2 le permet déjà côté LM Studio)._

- **2.6 — Timeouts mal calibrés pour l'oral** — `llm_client.py:486+` : `LLM_TIMEOUT_SECONDS=3600` (1 h !), `CLAUDE_CLI_TIMEOUT_SECONDS=600`. Un appel qui stalle ne sera jamais coupé à temps pour une conversation orale. _Fix : timeout TTFT séparé (ex. 15–30 s) + timeout completion ; sur stall, fallback verbal rapide._

### 🟡 Améliorations

- **2.7 — Tool specs reconstruites à chaque appel** — `llm_client.py:825–826`. `order_specs` + injection codec à chaque `complete()` alors que la registry est stable par session. _Fix : précompiler une fois par session._
- **2.8 — Swap = nouveau `AsyncOpenAI`, pool HTTP perdu** — `llm_swap.py:255–256`. Reconnexion TCP après chaque swap. _Fix : réutiliser le client httpx sous-jacent quand base_url inchangé._
- **2.9 — Probe HTTP fallback 3–6 s** sur chemin d'erreur SDK — `llm_swap.py:230–232`. Acceptable (recovery), à borner par timeout court.

### Mesure d'abord (P1)

- Instrumenter TTFT + latence end-to-end par turn (les events debug existent — agréger).
- Distribution des retries de validation en réel.
- Overhead subprocess Claude CLI isolé (spawn vs inférence).

## 3. Sub-agents & tools

**Pire cas chemin heureux : ~6–8 s d'attente orale (3 appels LLM séquentiels par sub-task simple). Pire cas erreur : 20–30 s de silence.**

### 🔴 Bloquants

- **3.1 — Stall sur erreur d'outil persistante : silence 20–30 s** — `runner.py:219–233` + `runner.py:1564–1568`. `stall_count` ne reset **que** sur résultat outil réussi ; si l'outil échoue toujours et que le modèle alterne `progress`, la boucle court jusqu'au cap (50 itérations). _Fix : reset `stall_count` quand l'`error_code` change (nouvelle tentative réelle) ; baisser `_STALL_FORCE_THRESHOLD` 4→3._

- **3.2 — Un seul tool call par réponse LLM = appels LLM séquentiels** — `runner.py:1499` + `actions.py`. Contrat « une action par tour » : Gmail + web fetch = 3 round-trips LLM au lieu d'un appel avec dispatch parallèle. ~9 s vs ~5 s idéal. _Fix lourd (schéma union d'actions) — à évaluer ; au minimum, mesurer la distribution d'itérations par type de tâche._

### 🟠 Importants

- **3.3 — `select_tools` + catalogue re-rendu à CHAQUE itération** — `runner.py:1750–1751`. Goal immuable pendant la run → résultat identique 50 fois ; catalogue JSON Schema (~20 KB) réinjecté dans le prompt à chaque iter → tokens gaspillés **et prefix-cache cassé** (renforce 2.3). _Fix : cacher `advertised_tools` par goal (5 lignes)._

- **3.4 — Timeouts LLM/outils non différenciés par type de tâche** — `config.py:57,68` (CLI 600 s, backend 3600 s), `config.py:218,264` (Tavily 15 s, MCP 30 s global). Recherche longue timeoutée prématurément sur CLI ; weather attend 30 s comme Gmail. Échec = `done(failed)` sec, sans retry ni dégradation. _Fix : budget par task-type (placeholder issue 0050) + timeout par tool ; sur timeout outil, retourner résultat partiel plutôt qu'erreur._

- **3.5 — Burst de tâches : aucune visibilité de queue** — `config.py:131` + `task_scheduler.py:138–152`. `MAX_RUNNING_TASKS=3` ; tâche #5 reste `pending` sans aucun feedback oral/HUD jusqu'à promotion. _Fix : exposer `queued_count()`/position pour que la synthèse puisse le dire ; envisager cap 5._

### 🟡 Améliorations

- **3.6 — Estimation tokens `len//4` sous-estime le français (~25–60 %)** — `runner.py:241–245`. `done(degraded, token_cap)` prématuré possible. _Fix : tiktoken ou ratio 3.5._
- **3.7 — Runner god object 2864 lignes, boucle `_run` ~700 lignes** — `runner.py:944–1670`. Déjà au backlog prod-hardening ; noté ici car ça bloque l'optimisation du chemin chaud (3.2, 3.3). _Refactor : extraire ActionHandlers + ToolAdvertiser._
- **3.8 — Sessions MCP : OK** — `connectors/mcp/manager.py:121–154`. Session cachée au boot, reconnect sur crash. Rien à faire.

## 4. WS & frontend

**Risque principal : un client WS lent/mort bloque l'orchestrateur entier (fan-out await sans timeout).**

### 🔴 Bloquants

- **4.1 — Fan-out WS sans timeout : un client lent gèle tout** — `event_bus_v2.py:218–226` + `ws_router.py:199–216`. `emit_event()` fait `await emitter(payload)` séquentiellement par fenêtre connectée (HUD + debug), sans timeout. Une fenêtre zombie (réseau coupé sans close) bloque `speech_delta`, `assistant_msg`, `task_updated` → **app entière freeze**. _Fix : `wait_for(emitter(payload), timeout=1–2 s)` + éviction de l'emitter mort ; idéalement queue par client avec drop-oldest._

- **4.2 — `send_bytes` TTS awaité chunk par chunk, sans buffer** — `ws_router.py:1692`. Client lent à lire → la synthèse attend l'écriture TCP de chaque chunk ; pas de pipeline synthèse/envoi. _Fix : queue locale de N chunks entre Kokoro et le WS (producteur/consommateur)._

### 🟠 Importants

- **4.3 — Un event debug JSON par chunk audio sur le chemin chaud** — `ws_router.py:1700–1712`. ~44 events/10 s de parole : sérialisation + ring buffer + fan-out + write JSONL chacun. _Fix : un event `audio_batch` tous les N chunks ou 100 ms._

- **4.4 — `emit_debug` sérialise + flush JSONL même Debug View fermée** — `debug_log.py:264–319`. `_write_to_file_sink` (write+flush disque par event) + `_enforce_retention` (re-dump JSON pour mesurer la taille) sur CHAQUE event, subscribers ou pas. _Fix : batch writes (flush toutes les 100 ms) ; skip JSONL si pas de subscriber et log désactivé._

- **4.5 — `speech_delta`/`reasoning_delta` non batchés côté backend** — frontend throttle déjà via rAF (`activityFeedStore.ts:1–78`, issue 0073) mais le backend émet un frame WS par token/segment. _Fix : buffer backend 50 ms avant émission._

### 🟡 Améliorations

- **4.6 — Orb WebGL : rAF 60 FPS même idle** — `ConscienceOrb.tsx`. GPU à fond en permanence ; contention main-thread pendant streaming. _Fix : throttle 1–5 FPS après 5 s idle._
- **4.7 — Reconnect WS : `audio_end` perdu → buffer audio ouvert** — `useWebSocket.ts:24–105`. Backoff OK (500 ms→10 s) mais rejoin mid-TTS sans resynchro → pop/coupure. _Fix : reset audio player à la reconnexion (audio_abort implicite)._
- **4.8 — Deux fenêtres Tauri = double fan-out de tous les events** — `tauri.conf.json:13–34`. La fenêtre debug reçoit aussi les events HUD ; combiné à 4.1, double le risque. _Fix : filtrage par type d'event par emitter._
- **4.9 — Mic : pas de buffer local pendant micro-coupure WS** — `useMicCapture.ts:149–158`. Frames drop pendant reconnect → mots tronqués au STT. Faible impact. _Fix : buffer local borné (200–300 ms) avec drop au-delà._

## 5. Fiabilité transverse

**8 patterns critiques non couverts par le backlog prod-hardening : erreurs avalées, FSM bloqué, timeouts manquants, fuites mémoire.** (Boot double-seed, backchannel drop, v1/v2 overlap, god objects : déjà au backlog 2026-06-09, non redupliqués.)

### 🔴 Bloquants

- **5.1 — Tâches TTS sans gestion d'erreur : audio fantôme** — `ws_router.py:231–246` (proactive) + `:1534` (main). `create_task(_synthesize_and_stream(...))` ; le done_callback ne fait que retirer de `active_tts` — exception jamais lue. Bob « parle » côté backend, aucun audio côté client, aucun log. _Fix : done_callback qui lit `task.result()` + event client sur échec._

- **5.2 — EventBus : exceptions subscribers silencieuses** — `event_bus.py:84`. `create_task(self._run_subscriber(...))` sans done_callback. Si le handler proactivity crash, `task_completed` se perd → sub-task jamais annoncée oralement (stall perçu). _Fix : done_callback global qui logge avec contexte._

- **5.3 — Flusher proactif peut mourir invisiblement** — `orchestrator.py:1234, 1271`. Si `_flush_proactive_loop` crash, la queue proactive stalle à jamais, sans signal. _Fix : done_callback + relance ou health flag._

- **5.4 — FSM coincé en BOB_SPEAKING après exception say-path** — `voice_loop.py:771` + `_finalize_say:806–809`. Si `_finalize_say` échoue lui-même (WS fermée), pas de hard-reset → invariant « jamais deux turns en bob_speaking » violé au prochain voice_start. _Fix : force-reset FSM vers IDLE dans le handler d'exception._

- **5.5 — Race voice_start : deux loops full-duplex simultanées** — `voice_loop.py:602–604`. Si `existing.stop()` lève, `session["voice_loop"]` n'est jamais nettoyé → second voice_start = deux loops, deux FSM divergents. _Fix : clear le slot AVANT le stop, stop sous `suppress`._

- **5.6 — Awaits réseau sans timeout sur chemins critiques** — `orchestrator.py:469` (summary regen = appel LLM), `:1461` (chat proactif), `ws_router.py:1617, 1656` (tts.preload, synthesize_stream). LLM/TTS qui freeze = turn perdu pour toujours, zéro signal client. _Fix : `asyncio.timeout` partout + fallback verbal ; rejoint 2.6 (watchdog de turn global)._

### 🟠 Importants

- **5.7 — Écriture sélection LLM non atomique** — `llm_selection_store.py:171–173`. `write_text` sync, crash mid-write = JSON corrompu = sélection perdue au reboot. _Fix : write-temp + `os.replace`._
- **5.8 — `_sessions` dict unbounded + `active_tts` qui s'accumule** — `ws_router.py:117`. Déconnexions sales jamais purgées ; fuite mémoire sur sessions longues. _Fix : cleanup périodique (sessions inactives 30 min) + purge des tasks done._
- **5.9 — `_ws_emitters` garde des références mortes** — `event_bus_v2.py:98`. Emitter qui throw n'est jamais retiré. _Fix : éviction au premier échec (même fix que 4.1)._
- **5.10 — Échec MCP startup silencieux** — `main.py:220–224`. Boot continue, tous les tools MCP morts, découvert seulement au premier sub-task. _Fix : warn fort + flag exposé dans /health._
- **5.11 — Échec de persistance d'un turn voix silencieux** — `voice_loop.py:1016`. Turn entendu mais absent de l'historique, sans signal. _Fix : event client `voice_persist_failed`._
- **5.12 — TaskStore : écritures SQLite sync sur le chemin chaud du runner** — `task_store.py`. `append_message`/`set_state` à chaque step ; disque lent = jitter event loop. _Fix : WAL mode + batch async._

### 🟡 Améliorations

- **5.13 — Preload Kokoro bloque le lifespan au boot** — `main.py:375–382`. 30 s+ premier boot, WS clients pendent pendant ce temps. Recoupe 1.8 ; partiellement au backlog. _Fix : preload en background task après yield._
- **5.14 — Aucun watchdog de turn** — transverse. Rien ne détecte un turn qui ne revient jamais. _Fix : wall-clock par turn + event `turn_timeout` + fallback verbal._

## Synthèse & priorisation

### Lecture d'ensemble

Le chemin critique oral (fin de parole → premier son) empile aujourd'hui des attentes **séquentielles** alors que presque tout pourrait être parallèle : finalize STT (0.5–2 s) → grace windows Thinker/Draft (jusqu'à 2 s) → commit gate → appel Jarvis (prefill complet car prompt non cache-friendly) → TTS phrase par phrase. S'y ajoutent deux risques systémiques : un client WS lent peut geler l'orchestrateur entier, et une famille d'erreurs avalées (TTS, EventBus, flusher, FSM) transforme des bugs ponctuels en stalls silencieux — le pire scénario pour une app orale.

### Top 10 par ROI (impact / effort)

| # | Quoi | Réf. | Gain estimé | Effort |
|---|------|------|-------------|--------|
| 1 | Paralléliser cancel Thinker/Draft + grace cap 250 ms (et **zéro grace sur barge-in**) | 1.2, 1.3 | jusqu'à −2 s endpoint, barge-in <300 ms | S |
| 2 | Lancer STT finalize en parallèle, ne pas bloquer le say-path | 1.1 | −0.5 à −2 s | M |
| 3 | Timeout + éviction sur le fan-out WS (`emit_event`) | 4.1, 5.9 | supprime le freeze app entière | S |
| 4 | Done_callbacks partout (`TTS`, EventBus, flusher) + force-reset FSM + fix race voice_start | 5.1–5.5 | supprime les stalls silencieux | S–M |
| 5 | Pipeliner TTS (phrase N+1 en synthèse pendant que N streame) + buffer send_bytes | 1.6, 4.2 | −250 ms/phrase | M |
| 6 | Prompt cache-friendly : fragments stables d'abord, catalogue tools caché par goal | 2.3, 3.3 | −0.5 à −1 s de prefill/turn local | M |
| 7 | Watchdog de turn + timeouts TTFT courts (15–30 s) sur tous les awaits LLM/TTS | 5.6, 5.14, 2.6 | aucun turn perdu sans signal | M |
| 8 | Stall guard runner : reset sur changement d'erreur + threshold 4→3 | 3.1 | silence erreur 30 s → 15 s | XS |
| 9 | Warmup boot complet (STT + TTS + role clients) en background après yield | 1.8, 5.13 | premier turn −2 à −5 s | S |
| 10 | Batch events chemin chaud (audio_chunk, speech_delta) + skip JSONL sans subscriber | 4.3–4.5 | CPU/jitter backend | S |

### À mesurer AVANT d'optimiser (instrumentation, ~1 jour)

Les events debug existent déjà — il manque l'agrégation :
1. **Time-to-first-audio par turn** (endpoint → premier `audio_chunk`), décomposé : finalize / freeze / gate / TTFT Jarvis / premier chunk TTS.
2. **Taux de `draft_hit`** (si <60 %, le speculative draft coûte plus qu'il ne rapporte).
3. **Distribution des retries de validation** (multiplicateur ×2–3 réel ou théorique ?).
4. **P95/P99 de `accept_frame` STT** par frame.

### Hors scope court terme (acté, pas oublié)

- Refactor runner/orchestrator god objects (3.7) — prérequis aux optimisations profondes du runner, déjà au backlog.
- Multi-tool-call par réponse LLM (3.2) — changement de contrat, gros chantier.
- Daemon Claude CLI (2.1) — à n'attaquer que si Claude CLI reste un provider voix ; sinon documenter « LM Studio pour l'oral ».
- Atomicité stores + fuites mémoire sessions (5.7, 5.8, 5.12) — fiabilité long-run, pas latence.
