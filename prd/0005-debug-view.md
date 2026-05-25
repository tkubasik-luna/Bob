# 0005 — Debug View (vue debug temps réel via raccourci clavier)

## Problem Statement

En tant que développeur unique de Bob, l'auteur travaille en permanence sur l'orchestrateur Jarvis, le pipeline LLM (LM Studio), le scheduler de sub-tasks, le bridge TTS Kokoro et la couche WebSocket. Quand quelque chose se passe mal ou différemment de ce qu'il attend — Jarvis répond hors sujet, une sub-task spawn alors qu'elle ne devrait pas, le TTS reste bloqué en `preparing`, une erreur silencieuse coupe une réponse en deux — il n'y a aujourd'hui aucune surface dans l'app pour voir *ce qui se passe en temps réel*.

Les seules sources d'information disponibles aujourd'hui :
- Le terminal où tourne `pnpm tauri dev` / `uvicorn`, qui mélange logs structlog JSON, traces stack, et stdout de Vite — illisible en pratique pour suivre un turn user à un autre.
- Le fichier `logs/llm-YYYY-MM-DD.jsonl` qui contient les LLM calls mais qu'il faut `tail -f` à la main et qui ne couvre pas le reste du flow.
- Le WebSocket user-facing `/ws` qui ne transporte que ce que la UI a besoin d'afficher (assistant_msg, task_updated, etc.) — pas les prompts LLM, pas les décisions de spawn sub-task, pas les internals.

Résultat : pour diagnostiquer un bug ou comprendre une décision Jarvis bizarre, l'auteur doit jongler entre 2-3 fenêtres terminal, parser du JSON à l'œil, ouvrir `logs/llm-*.jsonl` dans un éditeur, et reconstituer mentalement la chronologie. Friction élevée, cycle de debug lent.

## Solution

Une **vue debug** dédiée s'ouvrant dans une fenêtre Tauri séparée (`?ui=debug`) via un raccourci clavier `Cmd+Shift+D` depuis la fenêtre Sphere. Elle affiche en temps réel un feed chronologique d'**events humainement compréhensibles** couvrant tout le flow interne de Bob : input utilisateur, décisions Jarvis, calls LLM avec prompts/réponses, lifecycle des sub-tasks, output assistant, pipeline TTS, événements WS, erreurs.

Chaque event suit une **envelope structurée uniforme** `{ts, category, severity, source, summary, payload, turn_id, correlation_id?}` :
- `summary` est une ligne humaine écrite par le backend (ex: `Jarvis a reçu 42 chars de l'user`, `LLM call démarré (4500 tokens prompt)`, `Sub-task 'check_calendar' créée`).
- `payload` contient le détail brut (messages array LLM complet, response complète, exception trace) consultable au click.
- `turn_id` permet de regrouper visuellement tous les events déclenchés par un même user turn (y compris ceux émis depuis les sub-tasks spawnées dans ce turn).
- `correlation_id` lie les paires `*_start` / `*_end` des ops longues (LLM call, sub-task run).

Le frontend reçoit un firehose backend et filtre côté UI via 7 chips de catégorie (`input` / `llm` / `decision` / `task` / `output` / `voice` / `system`) + un seuil de severity (`trace` / `debug` / `info` / `warn` / `error`). Le feed défile en tail-style (newest en bas, auto-scroll, pause auto si scroll up). Le payload se déplie inline au click sur une ligne.

Un ring buffer mémoire backend (~2000 derniers events) est replayé au connect de la debug WS, ce qui rend la fenêtre debug utile même si elle est ouverte mid-session ou reload via HMR Vite.

## User Stories

1. En tant que développeur, je veux ouvrir/fermer la vue debug via le raccourci `Cmd+Shift+D` depuis la fenêtre Sphere sans devoir cliquer dans un menu, pour qu'inspecter le runtime soit aussi rapide que d'ouvrir DevTools dans Chrome.
2. En tant que développeur, je veux que la fenêtre debug soit déjà connectée au backend au moment où je l'ouvre, pour ne pas rater les events qui se sont passés avant que je presse le raccourci.
3. En tant que développeur, je veux voir chaque message envoyé par l'utilisateur apparaître comme une ligne lisible (`User envoie: "Hello"`), pour confirmer que mon input a bien été reçu côté backend.
4. En tant que développeur, je veux voir le moment où Jarvis commence à réfléchir et le moment où il termine, pour mesurer visuellement la latence de l'orchestrateur turn par turn.
5. En tant que développeur, je veux voir chaque call LLM en deux events (start + end) liés par un `correlation_id`, pour repérer immédiatement les calls qui hang ou qui prennent trop longtemps.
6. En tant que développeur, je veux que l'event `llm_call_start` contienne le full messages array envoyé au LLM, pour pouvoir copier-coller le prompt exact et le rejouer hors-app si besoin.
7. En tant que développeur, je veux que l'event `llm_call_end` contienne la response complète, le model name, le nombre de tokens in/out, et la latency en ms, pour diagnostiquer rapidement les régressions de coût ou de qualité.
8. En tant que développeur, je veux voir une ligne dédiée quand Jarvis décide de spawner une sub-task, indiquant le titre et le goal de la sub-task, pour comprendre pourquoi telle sub-task a été lancée.
9. En tant que développeur, je veux suivre tout le lifecycle d'une sub-task (pending → running → progress → ask_user → done | failed) comme des events successifs, pour repérer les transitions bloquées.
10. En tant que développeur, je veux que les events émis depuis l'exécution d'une sub-task (LLM calls de la sub-task, ask_user, etc.) partagent le même `turn_id` que le user_msg qui les a déclenchés, pour pouvoir reconstituer la chronologie complète d'un turn.
11. En tant que développeur, je veux voir chaque message assistant envoyé à l'utilisateur (`Bob répond: "..."`) avec, dans le payload, les blocs `ui` structurés et le flag `proactive`, pour vérifier ce qui a été effectivement émis sur le WS user-facing.
12. En tant que développeur, je veux voir les transitions du pipeline TTS (`tts_preparing`, `tts_ready`, `audio_start`, `audio_end`, `audio_error`) en catégorie `voice`, pour diagnostiquer les blocages Kokoro ou les coupures audio.
13. En tant que développeur, je veux pouvoir masquer la catégorie `voice` d'un click si je debug un problème de logique pure et que les events audio polluent ma vue.
14. En tant que développeur, je veux voir les connect / disconnect du WebSocket user-facing dans la vue debug, pour confirmer une reconnexion après une coupure réseau.
15. En tant que développeur, je veux que toutes les erreurs structlog de niveau WARN ou supérieur apparaissent automatiquement comme events de catégorie `system`, même si je n'ai pas explicitement instrumenté le site qui a planté.
16. En tant que développeur, je veux filtrer le feed par niveau de severity via un dropdown (afficher uniquement `>= info`, ou `>= warn` pour ne voir que les problèmes), pour calmer le bruit selon ce que je cherche.
17. En tant que développeur, je veux que la severity `trace` soit cachée par défaut (audio chunks et autres events haute fréquence), pour ne pas être inondé à l'ouverture.
18. En tant que développeur, je veux toggle chaque catégorie individuellement via 7 chips dans la toolbar, pour me concentrer sur une couche du système à la fois.
19. En tant que développeur, je veux mettre le feed en pause via un bouton (ou la touche Space) quand je suis en train de lire un event, pour ne pas être scrollé hors de ma ligne par l'arrivée d'événements suivants.
20. En tant que développeur, je veux pouvoir clear le feed côté UI sans toucher au buffer backend, pour repartir d'une vue propre avant de reproduire un bug, tout en gardant l'historique côté serveur si je rouvre la fenêtre.
21. En tant que développeur, je veux que le feed défile en mode tail (newest en bas, auto-scroll), pour conserver l'intuition `tail -f` que j'ai depuis 15 ans de terminal.
22. En tant que développeur, je veux que l'auto-scroll se mette en pause automatiquement si je scroll vers le haut, avec un badge "N nouveaux events" pour reprendre, pour pouvoir lire un event passé sans être ramené en bas à chaque tick.
23. En tant que développeur, je veux cliquer sur une ligne d'event pour déplier le payload JSON inline juste en dessous, sans changer de pane, pour passer du résumé au détail sans perdre le contexte chronologique.
24. En tant que développeur, je veux que le payload JSON déplié soit pretty-printed avec syntax highlighting basique, pour le scanner rapidement à l'œil.
25. En tant que développeur, je veux voir le `turn_id` affiché sous forme de petit chip coloré sur chaque ligne, pour repérer visuellement les events d'un même turn user.
26. En tant que développeur, je veux que cliquer sur le chip `turn_id` d'une ligne highlight toutes les autres lignes partageant le même `turn_id`, pour suivre la trace complète d'un turn d'un coup d'œil.
27. En tant que développeur, je veux que chaque ligne soit colorée selon sa severity (warn = ambre, error = rouge, trace = gris désaturé, info/debug = neutre), pour repérer instantanément les erreurs dans le flux.
28. En tant que développeur, je veux qu'un chip coloré identifie la catégorie de chaque ligne (badge type DevTools), pour scanner le feed en groupes logiques.
29. En tant que développeur, je veux que la vue debug utilise la font monospace `JetBrains Mono` déjà chargée par l'app, pour aligner les timestamps et faciliter la lecture chronologique.
30. En tant que développeur, je veux que les filtres de catégorie et severity soient initialisés à un défaut sain (toutes catégories ON, severity ≥ info) à la première ouverture, pour n'avoir rien à configurer avant de voir du contenu utile.
31. En tant que développeur, je veux que la fenêtre debug, quand je presse `Cmd+Shift+D` une seconde fois, se hide (et non se ferme/détruise), pour préserver le buffer affiché et l'état des filtres pour la prochaine ouverture.
32. En tant que développeur, je veux que la fenêtre debug soit indépendante de la fenêtre Sphere (déplaçable sur un second écran, redimensionnable), pour pouvoir afficher Bob et le debug côte à côte pendant une session.
33. En tant que développeur, je veux que les events émis avant l'ouverture de la fenêtre debug (ring buffer backend) soient replayés en bloc au connect, taggés avec un flag `replayed: true`, pour distinguer historique et temps réel.
34. En tant que développeur, je veux que les events `replayed` apparaissent dans le feed comme les autres (pas de section "historique" séparée), parce que la valeur du debug est dans la chronologie continue.
35. En tant que développeur, je veux que la vue debug montre les events de TOUTES les sessions WS actives (pas seulement la session courante du Sphere HUD), pour debug les cas multi-session ou les leftover de la session précédente.
36. En tant que développeur, je ne veux PAS de censure ni de masquage de secrets / tokens / prompts complets dans la vue debug, parce que c'est un outil dev uniquement, jamais shippé à un user final.
37. En tant que développeur, je veux que le raccourci `Cmd+Shift+D` fonctionne seulement quand la fenêtre Sphere a le focus (window-scoped), pour ne pas avoir à gérer une permission Accessibility macOS pour un global shortcut Tauri.
38. En tant que développeur, je veux que le raccourci `Cmd+Shift+D` ne se déclenche PAS quand je tape dans l'InputField de Sphere, pour ne pas ouvrir la fenêtre debug par accident en discutant avec Bob.
39. En tant que développeur, je veux que l'instrumentation backend soit en place sur ~20 sites métier dès la v1 (user_msg, thinking start/end, LLM start/end, spawn sub-task, sub-task lifecycle complet, assistant_msg, TTS pipeline, WS lifecycle), pour que la vue soit immédiatement utile et non un squelette vide.
40. En tant que développeur, je veux qu'ajouter une nouvelle instrumentation soit une ligne `emit_debug(...)` triviale à inscrire dans n'importe quel module backend, pour ne pas être freiné quand je veux logguer un nouveau point d'intérêt.
41. En tant que développeur, je veux que le ring buffer backend ait une taille bornée (~2000 events) pour ne pas faire fuir la RAM en session longue.
42. En tant que développeur, je veux que la connection / déconnection de `/ws/debug` ne perturbe pas les autres connections WS de l'app (chat user, sub-tasks), pour pouvoir reload la debug window sans casser Bob.
43. En tant que développeur, je veux que le pipeline d'émission `emit_debug()` soit non-bloquant : si la WS debug n'a aucun client connecté ou si elle est lente, ça ne doit pas ralentir l'orchestrator ou le LLM call.
44. En tant que développeur, je veux que la vue debug fonctionne en environnement `pnpm tauri dev` (le seul contexte où le mécanisme de fenêtres multiples Tauri est disponible) ; le contexte `pnpm dev` web-only peut afficher un placeholder ou simplement ignorer l'absence de Tauri APIs.
45. En tant que développeur, je veux pouvoir étendre plus tard le set de filtres / le set d'events sans casser l'API existante, donc l'envelope `{ts, category, severity, source, summary, payload, turn_id, correlation_id?}` doit être souple (champs optionnels possibles dans `payload`).

## Implementation Decisions

### Architecture générale

- Vue debug = nouvelle fenêtre Tauri dédiée routée par `?ui=debug`, cohabitant avec `?ui=new` (Sphere) et `?ui=legacy` (ChatView). Single React bundle partagé, dispatch par query param dans `App.tsx`.
- Fenêtre pré-déclarée dans la config Tauri avec `visible: false`. Toggle show/hide via une commande Tauri Rust (`toggle_debug_window`) appelée depuis un `keydown` listener dans la fenêtre Sphere.
- Backend expose un nouveau endpoint WebSocket `/ws/debug` distinct du `/ws` user-facing existant. Pas d'authentification — Bob est local-only.

### Modules backend (nouveaux)

**`debug_log.py`** — deep module pur, sans coupling FastAPI, testable en isolation :
- Définit le type `DebugEvent` (dataclass ou Pydantic) avec les champs `ts` (ISO 8601 ms), `category` (Literal des 7 valeurs), `severity` (Literal des 5 valeurs), `source` (str dotted path), `summary` (str humain), `payload` (dict free-form), `turn_id` (UUID str), `correlation_id` (Optional UUID str), `replayed` (bool, défaut False).
- Expose le helper `emit_debug(category, severity, source, summary, payload=None, correlation_id=None)` qui crée un event, le pousse dans le ring buffer et le publie sur un async pub-sub interne.
- Maintient un `collections.deque(maxlen=2000)` comme ring buffer global.
- Expose un `ContextVar` `current_turn_id` propagé via `contextvars` standard Python — chaque appel `process_user_message` (ou équivalent point d'entrée d'un turn) génère un nouvel UUID et le set dans le ContextVar pour la durée du turn, y compris les coroutines descendantes (sub-tasks spawnées via `asyncio.create_task` doivent hériter du contexte, ce que `contextvars` fait nativement).
- Expose une fonction `subscribe() -> AsyncIterator[DebugEvent]` que le routeur WS consomme, qui yield d'abord le snapshot du ring buffer (events taggés `replayed=True`) puis stream les nouveaux events à mesure qu'ils arrivent.
- Installe un structlog processor / handler qui auto-forward tout log record de niveau WARN ou ERROR vers `emit_debug(category="system", severity="warn"|"error", source=logger_name, summary=event_msg, payload=record_fields)`. Filet de sécurité pour les erreurs non instrumentées.
- L'émission est non-bloquante : la publication interne utilise `asyncio.Queue` par subscriber, avec un overflow strategy `drop_oldest` si un client est trop lent pour ne jamais bloquer le producteur.

**`ws_debug.py`** — thin module, route FastAPI :
- Déclare `@router.websocket("/ws/debug")`.
- À chaque connexion : appelle `subscribe()` et stream chaque event en JSON. Replay snapshot d'abord, puis live.
- Gère la déconnexion proprement (cleanup du subscriber dans le buffer pub-sub).

### Sprinkle backend (modifications de modules existants)

Pose des appels `emit_debug(...)` aux sites suivants — listés par module pour faciliter la lecture, summary humain en français par défaut :

- `orchestrator.py` : entrée `process_user_message` (cat=`input`, sev=`info`, génère et set `turn_id`) ; début thinking (cat=`decision`, sev=`info`) ; fin thinking (cat=`decision`, sev=`info`) ; décision `_spawn_subtask` (cat=`decision`, sev=`info`, payload contient title + goal) ; sortie `_emit_proactive` ou équivalent (cat=`output`, sev=`info`, payload contient speech + ui blocks + proactive flag).
- `llm_client.py` : avant l'appel HTTP au LLM (cat=`llm`, sev=`info`, summary `LLM call démarré (N tokens prompt, model=X)`, payload contient le full `messages` array et le model name, génère `correlation_id`) ; après réponse (cat=`llm`, sev=`info`, summary `LLM call terminé en Xms (N tokens response)`, payload contient response complète + latency_ms + tokens_in/out, reprend le même `correlation_id`).
- `sub_agent_runner.py` : `_handle_done` (cat=`task`, sev=`info`) ; `_handle_progress` (cat=`task`, sev=`debug`) ; `_handle_ask_user` (cat=`task`, sev=`info`) ; `_fail` (cat=`task`, sev=`warn`).
- `task_scheduler.py` : `_promote` pending→running (cat=`task`, sev=`info`) ; `cancel` (cat=`task`, sev=`info`).
- `ws_router.py` : connect (cat=`system`, sev=`info`) ; disconnect (cat=`system`, sev=`info`) ; TTS `tts_preparing` (cat=`voice`, sev=`info`) ; `tts_ready` (cat=`voice`, sev=`debug`) ; `audio_start` (cat=`voice`, sev=`debug`) ; `audio_end` (cat=`voice`, sev=`debug`) ; `audio_error` (cat=`voice`, sev=`warn`).

### Frontend (nouveaux fichiers + modifications)

**Nouveaux :**
- `frontend/src/types/ws-debug.ts` — mirror du type `DebugEvent` backend, plus types pour le state UI (filters, paused, etc.).
- `frontend/src/hooks/useDebugWs.ts` — deep hook, encapsule l'ouverture du WS `/ws/debug`, le buffer local d'events (capped, ex: 5000), la gestion du pause/resume, l'expose en `{events, paused, setPaused, clear}`. Aucun couplage React-DOM, peut être testé en isolation avec une mock WS.
- `frontend/src/components/debug/DebugView.tsx` — composant root, monte le hook, compose `<DebugToolbar>` + feed.
- `frontend/src/components/debug/DebugToolbar.tsx` — 7 chips cliquables pour catégories, dropdown pour seuil severity, bouton pause (label change selon état), bouton clear. Touche Space = toggle pause global.
- `frontend/src/components/debug/DebugRow.tsx` — une ligne event, gère son propre state `expanded`. Click → toggle expand, affiche payload JSON pretty-printed en dessous.

**Modifiés :**
- `frontend/src/App.tsx` — ajoute la branche `if (ui === "debug") return <DebugView />;` avant les autres.
- `frontend/src-tauri/tauri.conf.json` — ajoute l'entrée fenêtre `debug` avec `visible: false`, `width: 1024`, `height: 700`, `url: "/?ui=debug"`, `title: "Bob · Debug"`.
- `frontend/src-tauri/src/main.rs` — déclare la commande Tauri `toggle_debug_window` qui récupère `WebviewWindow` par label `debug` et appelle `.show()` / `.hide()` selon état actuel ; l'enregistre dans le `tauri::Builder`.
- `frontend/src/components/sphere/SphereUI.tsx` — installe un `keydown` listener au mount qui détecte `Cmd+Shift+D` (event.metaKey + event.shiftKey + event.code === "KeyD") et invoque la commande Tauri `toggle_debug_window` via `@tauri-apps/api`. Garde : ignore si `event.target` est un input/textarea/contenteditable.

### Schema sur le fil (`/ws/debug` → frontend)

Chaque message texte JSON envoyé par le backend respecte l'envelope :

```
{
  "ts": "2026-05-25T14:23:01.123Z",
  "category": "llm",
  "severity": "info",
  "source": "bob.llm_client.complete",
  "summary": "LLM call démarré (4523 tokens prompt, model=qwen2.5)",
  "payload": { "messages": [...], "model": "qwen2.5", "tokens_prompt": 4523 },
  "turn_id": "9f2a...",
  "correlation_id": "c8e1...",
  "replayed": false
}
```

`turn_id` et `correlation_id` sont des strings UUID (omis si non applicable côté `correlation_id`, toujours présent côté `turn_id` après le premier user_msg).

### Comportement UI

- Feed vertical, newest en bas, scroll auto au bottom si déjà en bas, pause auto-scroll si l'utilisateur a scrollé up de plus de quelques px. Badge flottant `↓ N nouveaux events` quand auto-scroll en pause, click pour scroller en bas et reprendre.
- Click sur une ligne → toggle expand : la ligne devient haute, affiche `payload` en JSON pretty-print (2-space indent) en dessous du summary, avec syntax highlighting léger (string en jaune, number en vert, key en bleu — au choix d'implémentation, libre si simple).
- Click sur le chip `turn_id` → toutes les lignes partageant ce `turn_id` reçoivent un outline / background différent pendant ~5s pour les highlight visuellement.
- Filtres par catégorie : 7 chips dans la toolbar, état `on/off`. Click toggle. Visuellement `off` = grisé.
- Filtre par severity : dropdown avec 5 valeurs, montre tout ce qui est `>= seuil`. Défaut `info`.
- Touche Space sur la fenêtre debug = toggle pause.
- Bouton clear = vide la liste locale `events`, n'envoie rien au backend.
- Persistance des filtres : pas de persistence v1 (re-set au défaut à chaque ouverture de session app).

### Style

- Background sombre uniforme (réutiliser `--hud-bg` ou définir une variable dédiée `--debug-bg`).
- Font `JetBrains Mono` partout (déjà chargée par le projet via le CSS racine).
- Couleurs severity : `--debug-warn` (ambre), `--debug-error` (rouge), `--debug-trace` (gris désaturé). Réutiliser les `--warn` / `--err` existants de `hud.css` si compatibles.
- Couleurs catégorie : 7 teintes distinctes pour les chips, à choisir au moment de l'implémentation pour distinguer visuellement (ex: input=bleu, llm=violet, decision=cyan, task=vert, output=jaune, voice=rose, system=gris).

## Out of Scope

- Aucune authentification ni protection du endpoint `/ws/debug` : Bob est local-only, l'endpoint est accessible à quiconque a accès à `127.0.0.1`.
- Aucun masquage ni redaction de secrets / tokens / contenus privés dans les events : tout est dumpé brut.
- Aucune persistance disque des events debug : le ring buffer est purement en RAM, perdu au restart du backend. Le fichier `logs/llm-*.jsonl` existant continue d'exister indépendamment.
- Aucun export depuis l'UI debug (JSON dump des events visibles) : à implémenter plus tard si besoin de partager un trace.
- Aucun champ de recherche texte free dans la toolbar : filtres par catégorie + severity uniquement en v1.
- Aucune virtualisation de liste (`react-window` / `react-virtual`) : pour des sessions de quelques heures à 2000 events visibles max, le rendu React natif suffit.
- Aucun timeline / swim-lane view : feed plat uniquement.
- Aucun toggle de catégorie / set d'events activable côté backend : le backend émet toujours le firehose complet. Le frontend décide ce qu'il affiche.
- Aucun support du raccourci `Cmd+Shift+D` quand l'app Bob n'a pas le focus (pas de Tauri global-shortcut plugin) : il faut focus la fenêtre Sphere d'abord.
- Aucune ouverture de la vue debug dans le contexte `pnpm dev` web-only (pas de Tauri APIs disponibles) : si le besoin se manifeste, on rajoutera un fallback (peut-être ouvrir un nouvel onglet browser avec `?ui=debug`).
- Aucun affichage Markdown / rich-text dans les `summary` : texte plain uniquement.
- Aucun replay côté frontend par session_id ou par filtre arbitraire : le backend replay tout le ring buffer au connect, le frontend filtre ensuite localement.
- Aucun event de catégorie `decision` ou `task` n'est ajouté au WS user-facing `/ws` : la vue debug ne change pas le contrat user-facing existant.
- Aucun changement de structure ou de payload du WS `/ws` user-facing : les messages déjà émis (assistant_msg, task_*, audio_*, etc.) restent tels quels. La vue debug duplique partiellement leur contenu via `emit_debug` aux mêmes sites, ce n'est pas un proxy.

## Further Notes

- **Pattern contextvars** : `current_turn_id` doit être set à l'entrée de `process_user_message` (ou tout autre point d'entrée d'un turn user) et propagé naturellement via `contextvars` Python. Toute coroutine spawnée via `asyncio.create_task` à l'intérieur du turn hérite du contexte (comportement standard). Si une sub-task est démarrée hors du turn courant (proactivité indépendante), un nouveau `turn_id` doit être généré au point d'entrée correspondant.
- **Cohérence avec le pattern replay existant** : Le ring buffer + flag `replayed: true` reprend le pattern utilisé par le store des sub-tasks aujourd'hui (les sub-tasks running/pending sont replayées au connect du WS user avec `replayed: true`). Le frontend pourra utiliser la même convention de styling discret pour les events replayés si désiré.
- **Coût rate des events** : LLM calls = ~quelques par turn ; sub-task lifecycle = ~5 events par sub-task ; TTS pipeline = ~5 events par speech segment + chunks audio (severity `trace`) ; user actions = sporadique. Ordre de grandeur d'un turn user typique = 20-50 events à l'info+ level, plus quelques centaines en trace. Le ring buffer de 2000 events couvre confortablement plusieurs dizaines de turns.
- **Compat HMR Vite** : la fenêtre debug en dev pnpm tauri dev sera reload par Vite à chaque édition de fichier React. Le WS se reconnecte automatiquement, le ring buffer backend replay l'historique → l'UX dev est continue.
- **Extension future possible** (notée pour mémoire, hors scope v1) : champ search texte, export JSON, persistence des filtres en localStorage, toggle catégorie côté backend pour bandwidth, virtualisation de liste si on monte à >10k events, mode "trace replay" qui rejoue un export d'events en temps simulé pour reproduire un bug.
- **Naming** : le module `debug_router.py` existant est un HTTP endpoint de smoke test TTS sans rapport ; les nouveaux modules `debug_log.py` et `ws_debug.py` ont des noms volontairement distincts pour éviter la confusion.
