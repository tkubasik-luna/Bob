# 0016 — Jarvis Temps Réel Full-Duplex (+ harnais d'attestation agent)

Source : `docs/investigations/2026-06-07-jarvis-realtime-agent.md` (investigation +
grill 2026-06-07, 36 décisions de design + 3 conflits code résolus + harnais conçu).
Inspiration : NVIDIA PersonaPlex (full-duplex), mais architecture **cascade-parallèle**
(Listen-Think-Speak + anticipation + inner-thoughts) — on garde le cerveau Jarvis, pas
de modèle S2S unifié.

## Problem Statement

Aujourd'hui Bob est un assistant **tour-par-tour** : l'utilisateur tape du texte, Jarvis
réfléchit, puis répond en voix (Kokoro TTS). Il n'écoute pas pendant qu'on parle, ne peut
pas être interrompu, n'anticipe rien, et ne réagit jamais spontanément. Le ressenti est
celui d'un distributeur requête→réponse, pas d'un interlocuteur vivant. Il n'existe
d'ailleurs aucune entrée micro/STT : la voix est en sortie uniquement.

Par ailleurs, l'utilisateur veut pouvoir **faire valider le système par l'agent (Claude)
en autonomie réelle, au fur et à mesure** de la construction — or un système full-duplex
est piloté par du hardware (micro, audio temps réel) qu'un agent ne peut ni produire ni
entendre, donc rien n'est aujourd'hui attestable sans humain.

## Solution

Transformer Jarvis en **agent vocal full-duplex temps réel** :

- **Micro toujours ouvert** quand le mode voix est ON (le toggle existant devient le
  kill-switch). Bob écoute pendant que l'utilisateur parle, transcrit en streaming, et
  **comprend avant la fin de la phrase**.
- **Penser en parallèle** : un *Thinker* de fond maintient en continu un état de la
  conversation (intention, variables, plan) pendant que l'utilisateur parle ; un *Draft*
  spéculatif pré-rédige la réponse sur le transcript partiel.
- **Réponse quasi-instantanée** au moment où l'utilisateur s'arrête : si le brouillon
  spéculatif tient, il est adopté immédiatement.
- **Barge-in** : l'utilisateur peut couper Bob en pleine phrase ; Bob s'arrête net,
  mémorise ce qu'il a déjà dit, et réécoute.
- **Backchannels** légers ("mm", "ok je vois") dans les pauses, pour un échange vivant.
- **Picker par-rôle** : chaque rôle LLM (Speaker, Thinker, Draft, sous-agent) est
  assignable indépendamment à Claude **ou** à un modèle LM Studio (local ou serveur
  distant), depuis les réglages — l'utilisateur arbitre lui-même le compromis
  latence/intelligence.
- **L'entrée texte reste disponible** (usage hybride voix + texte, zéro régression).

Et, transversalement, un **harnais d'attestation `bob` CLI** : un outil headless qui
pilote la vraie pile par le WebSocket, injecte des entrées en fixture (audio ou texte),
et **atteste par des assertions machine-lisibles** que chaque slice fonctionne — pour que
l'agent valide son travail en autonomie, sans micro ni oreille.

## User Stories

1. En tant qu'utilisateur, je veux activer un mode voix qui ouvre le micro en continu,
   afin de parler à Bob sans cliquer ni maintenir de touche.
2. En tant qu'utilisateur, je veux que Bob me transcrive en streaming pendant que je
   parle, afin qu'il commence à comprendre avant que je finisse.
3. En tant qu'utilisateur, je veux que Bob détecte la fin de mon tour intelligemment (pas
   juste sur un silence), afin qu'il ne me coupe pas quand j'hésite en milieu de phrase.
4. En tant qu'utilisateur, je veux que Bob réponde quasi-instantanément quand je m'arrête,
   afin que la conversation soit fluide.
5. En tant qu'utilisateur, je veux pouvoir couper Bob en pleine phrase juste en me mettant
   à parler, afin de reprendre la main sans attendre.
6. En tant qu'utilisateur, je veux que Bob se souvienne de ce qu'il avait déjà dit avant
   que je le coupe, afin que la suite de l'échange reste cohérente.
7. En tant qu'utilisateur, je veux que Bob place de brefs accusés de réception ("mm",
   "ok") pendant mes pauses, afin de sentir qu'il m'écoute.
8. En tant qu'utilisateur, je veux que Bob ne fasse pas ces backchannels par-dessus ma
   parole, afin de ne pas créer de cacophonie.
9. En tant qu'utilisateur, je veux que le micro ne capte pas la propre voix de Bob (écho),
   afin que Bob ne se réponde pas à lui-même.
10. En tant qu'utilisateur, je veux continuer à pouvoir taper du texte même en mode voix,
    afin de coller une URL ou un mot précis sans le dicter.
11. En tant qu'utilisateur, je veux que désactiver le mode voix ferme réellement le micro,
    afin de garder le contrôle sur ma vie privée.
12. En tant qu'utilisateur, je veux que Bob fonctionne en français, afin que la
    transcription et la compréhension soient correctes dans ma langue.
13. En tant qu'utilisateur avancé, je veux choisir, pour chaque rôle interne (Speaker,
    Thinker, Draft, sous-agent), s'il tourne sur Claude ou sur un modèle LM Studio, afin
    d'arbitrer moi-même latence vs intelligence.
14. En tant qu'utilisateur avancé, je veux pouvoir pointer un rôle vers un serveur LM
    Studio distant, afin de décharger un gros modèle sur une machine GPU pendant qu'un
    petit modèle reste local.
15. En tant qu'utilisateur avancé, je veux choisir le modèle exact et la taille de
    contexte par rôle, afin de calibrer chaque étage.
16. En tant qu'utilisateur, je veux que Bob ne sature pas ma RAM en chargeant trop de
    modèles, afin que la machine reste stable.
17. En tant qu'utilisateur, je veux être averti si ma sélection de modèles dépasse le
    budget mémoire, afin de corriger avant un crash.
18. En tant qu'utilisateur, je veux que choisir un modèle déjà chargé pour un rôle ne
    décharge pas inutilement les autres, afin d'éviter les rechargements lents.
19. En tant qu'utilisateur, je veux que mes échanges vocaux soient conservés (transcript
    + audio) pour rejouer/déboguer, afin d'améliorer le système.
20. En tant qu'utilisateur, je veux que cette conservation soit bornée (âge + taille),
    afin que le disque ne se remplisse pas indéfiniment.
21. En tant qu'utilisateur, je veux que le transcript final d'un tour vocal entre dans
    l'historique Jarvis, afin que la conversation persistante reste continue.
22. En tant qu'utilisateur, je veux voir l'état de la conversation (qui a la parole) dans
    le HUD, afin de comprendre ce que fait Bob en temps réel.
23. En tant qu'utilisateur, je veux des réponses dont la latence reste sous des cibles
    claires (barge-in <300 ms, premier audio <800 ms quand anticipé), afin que ça paraisse
    temps réel.
24. En tant que développeur/agent, je veux une commande unique qui boote un backend isolé,
    rejoue un scénario et rend un verdict, afin d'attester une slice sans configurer
    l'environnement à la main.
25. En tant que développeur/agent, je veux injecter une entrée utilisateur sous forme de
    texte (sans audio), afin d'attester l'orchestration de façon rapide et déterministe.
26. En tant que développeur/agent, je veux injecter une entrée sous forme de fichier audio,
    afin d'attester aussi la couche STT et le canal binaire de bout en bout.
27. En tant que développeur/agent, je veux que les assertions portent sur des invariants
    (état FSM atteint, latence, barge-in coupé à temps, rôle→modèle, texte committé ==
    texte prononcé) plutôt que sur le texte exact, afin que les tests ne soient pas flaky.
28. En tant que développeur/agent, je veux simuler un barge-in (parole injectée à t=X
    pendant que Bob parle), afin d'attester l'interruption.
29. En tant que développeur/agent, je veux pouvoir attester sans dépendre d'un LM Studio
    lancé (LLM fake scriptable), afin de tourner offline et en CI.
30. En tant que développeur/agent, je veux pouvoir lancer l'attestation contre de vrais
    LLM (option `--real`), afin de valider le vrai bout-en-bout quand c'est pertinent.
31. En tant que développeur/agent, je veux que chaque slice livre son scénario
    d'attestation et que sa definition-of-done soit "scénario vert", afin que les
    régressions soient vues tôt.
32. En tant que développeur/agent, je veux un verdict JSON + code de sortie non-zéro en
    cas d'échec, afin d'intégrer l'attestation à la CI et au verify automatique.
33. En tant que développeur/agent, je veux optionnellement vérifier l'intelligibilité de
    la voix de Bob (round-trip TTS→STT) en mode `--deep`, afin d'attester que ce qui est
    dit est compréhensible.
34. En tant que développeur, je veux valider très tôt que la capture micro + l'AEC
    fonctionnent dans le webview Tauri (spike), afin de basculer sur un fallback Rust si
    nécessaire avant d'avoir tout construit dessus.

## Implementation Decisions

### Périmètre & cible
- **Scope "tout d'un coup"** : audio I/O + Thinker + Draft + picker par-rôle + harnais
  d'attestation, dans le même PRD.
- **Cible matérielle** : Mac Apple Silicon 32 Go+ (mémoire unifiée).
- **Hybride voix + texte** : le path d'entrée texte existant est conservé, le full-duplex
  voix s'ajoute. Branchement commun en aval (un tour vocal et un tour texte convergent
  vers le même Speaker / say-path).

### Pipeline audio & STT
- **Capture** : frontend, via `getUserMedia({ audio: { echoCancellation: true } })` +
  AudioWorklet ; downsample 16 kHz mono ; **AEC géré par le webview** (WKWebView annule sa
  propre sortie). Le micro est possédé par la fenêtre HUD `new` et armé tant que le mode
  voix est ON.
- **Transport** : nouveau **canal WebSocket binaire client→serveur** transportant des
  **frames PCM 16 kHz mono s16le** (~20–40 ms). Le WS de Bob est aujourd'hui JSON-texte
  only : ajout d'un mode binaire.
- **STT** : backend, **whisper.cpp (Metal/CoreML)**, modèle par défaut **large-v3-turbo**
  (réglable). `faster-whisper` est écarté (CTranslate2 = CPU-only sur Mac). Module
  `SttEngine` à interface stable (frames → partiels + final), moteur swappable.
- **Endpointing** : hybride **VAD silence (~500–700 ms) + signal sémantique
  `user_turn_complete` émis par le Thinker** (pas de classifieur dédié au départ). Module
  `Endpointer` consommant les deux sources.

### Orchestration temps réel
- **`TurnFsm`** — FSM compact : `idle / user_speaking / thinking / bob_speaking` +
  transition **barge-in**. Le backchannel est une **action** émise pendant
  `user_speaking`, pas un état (pas d'état `overlap`).
- **Barge-in** (`BargeInController`) — déclenché par **VAD + fenêtre de confirmation
  ~200–300 ms** (filtre backchannels/bruits courts). Action : annuler le stream LLM en
  cours + le TTS, **committer dans l'historique le texte déjà prononcé**, repasser en
  `user_speaking`.
- **Backchannels** — courts, **dans les pauses** seulement, gated par le Thinker + un
  seuil de proactivité (logique inner-thoughts "when-to-speak").

### Penser en parallèle
- **`ThinkerLoop`** — sous-agent de fond (modèle mini 1–3B par défaut) tournant en continu
  sur le transcript partiel. Triple sortie : (a) un **snapshot d'état**
  `{ texte_corrigé, variables, plan_prochaine_étape }`, (b) le signal `user_turn_complete`
  (endpoint sémantique), (c) le déclenchement de backchannel.
- **`LiveTranscriptState`** — store en mémoire que le Thinker met à jour ; **lu par un
  provider pur `ThinkerStateProvider`** au moment de l'assemblage du prompt (même pattern
  que `StateBlockProvider` lisant `TaskStore`). L'assemblage de contexte reste pur et
  par-tour ; il est **déclenché par l'endpoint du FSM**, pas par un message texte.
- **`SpeculativeDraft`** — génère, sur le transcript partiel, un **texte de réponse brut
  hors codec** (pas un tool-call validé). À l'endpoint : **gate de commit** = fast-path
  préfixe (final ≈ préfixe du partiel → commit instantané) sinon garde de similarité
  légère (token-overlap/embedding) ; si divergent → jeté + regénération. La spéculation ne
  porte **que sur la réponse conversationnelle**, pas sur les tours qui dispatchent un
  outil.
- **`Speaker`** — adapte le say-path Jarvis existant : consulte le dernier snapshot
  Thinker, et **adopte le texte committé du Draft** quand il est validé (réinjecté dans le
  say-path normal → validation triviale → TTS).
- **Concurrence réelle** : Thinker, Draft et Speaker tournent sur des **modèles/hosts
  distincts** pour ne pas se sérialiser sur un même moteur d'inférence (un rôle peut aussi
  être sur Claude, qui n'a pas de contention locale).

### Modèles & picker par-rôle
- **`RoleSelectionStore`** — fait évoluer `LLMSelectionStore` d'une sélection globale unique
  vers une **map complète `{role: LLMSelection}`**. Rôles : `jarvis` (= Speaker) /
  `thinker` / `draft` / `subagent`. Chaque `LLMSelection` porte déjà
  `{ provider (claude_cli | lm_studio), base_url, lm_model, context_length }`. **Chaque rôle
  choisit Claude OU LM Studio**, et **chaque rôle a son propre `base_url`** (serveur
  potentiellement différent). Migration : la sélection globale persistée initialise les 4
  rôles ; décodage défensif conservé.
- **Factory par-rôle** — un builder par rôle (`build_<role>_client`) épinglant
  provider/base_url/model du rôle. `LMStudioClient` **route par paramètre `model`** vers le
  serveur du rôle. Le coordinateur de swap (`llm_swap`) rebuild **uniquement le client du
  rôle modifié** (au lieu des deux clients aujourd'hui). Endpoints `llm_router` étendus
  par-rôle (GET/PUT selection par rôle).
- **`ModelBudget`** — module pur estimant le **footprint** d'un modèle (taille fichier sur
  disque du GGUF/MLX + marge KV-cache ∝ `context_length`) et vérifiant la tenue **par
  host** : somme des footprints des modèles résidents ≤ **plafond**. Plafond local =
  RAM détectée (sysctl) − marge OS (~8 Go), avec **override + marge réglables** en
  settings. Pour un **host distant** (RAM non lisible) : plafond depuis settings si
  renseigné, sinon **skip** (try+catch OOM).
- **`LMStudioManager` v2** — remplace la politique **offload-first** (`load()` actuel évince
  tous les autres modèles) par un **multi-load budget-aware avec offload sélectif
  ref-compté** : on charge les modèles des rôles, on les garde résidents, on n'évince un
  modèle que lorsque **plus aucun rôle ne le référence** (sur le même host), et on refuse +
  avertit si le budget serait dépassé. Manager **par host**. (Revient en partie sur la
  décision robustesse du 2026-06-05 ; voir Further Notes.)

### Rétention & privacy
- **`VoiceRetentionPolicy`** — borne la persistance (transcripts + partiels + audio) par
  **âge + taille**, avec des **caps séparés** : audio (gros → cap taille serré ~1–2 Go),
  transcripts (légers → cap âge ~30 j), purge automatique. Réutilise le pattern de
  `EventRetentionPolicy`.
- **Persistance** : l'audio + les partiels + le transcript final sont écrits (debug/replay/
  tuning) ; le transcript final entre dans l'historique Jarvis comme un tour. Le toggle voix
  OFF coupe réellement la capture.

### Harnais d'attestation (`bob` CLI)
- **`bob` CLI** — nouvel entrypoint (console_script via `pyproject`), sous-commandes
  (`attest`, `say`, `scenario`…), **sortie JSON**, **exit non-zéro** à l'échec. Greenfield
  (aucun CLI aujourd'hui).
- **Drive layer** — **black-box sur le vrai WS/HTTP** : la CLI pilote un backend qui tourne,
  injecte les entrées, et **asserte sur le flux `/ws/debug`** (event bus existant).
- **Injection** — deux modes : `--text` (injecte un transcript, skip whisper → rapide +
  déterministe) et `--audio fixture.wav` (frames micro → STT → e2e ; dépend du canal
  binaire WS).
- **`AttestAssertions`** — assertions sur **invariants/contrats** : état FSM atteint, `say`
  émis, latence < cible, barge-in coupé < 300 ms, rôle R a servi modèle M, deliverable
  non-vide, **texte committé == texte prononcé**. Jamais le texte exact ; `temperature=0`
  où l'exactitude compte.
- **Validation TTS** — par défaut texte pré-synthèse + count/octets/timing des `audio_chunk`
  (couvre le barge-in = chunks coupés) ; option `--deep` = round-trip TTS→whisper→compare.
- **`ScenarioRunner`** — exécute des **scénarios déclaratifs YAML/JSON** : timeline
  d'événements timés (`inject à t=X`, `attendre état Y`) + assertions attendues ; **hook
  Python** en échappatoire pour cas complexes. Un scénario par slice, versionné.
- **`EphemeralBackend`** — boote un backend **isolé** (BOB_DATA_DIR temporaire, DB fraîche,
  port dédié), exécute, tear-down — en une commande, **zéro pollution** de l'état réel.
  `--external` (pointer un backend existant) prévu plus tard.
- **`FakeLlmBackend`** — backend LLM **scriptable déterministe** (réponses pilotées par le
  scénario), branché via le switch provider de la factory. **Défaut** de l'attestation
  (offline, déterministe, CI). Option `--real` pour les vrais LLM. Réutilise le pattern de
  fake du SDK déjà présent en tests.
- **Intégration** — chaque issue livre son scénario ; sa **DoD = `bob attest <scenario>`
  vert**. Se branche sur l'adversarial-verify d'`implement-feature-v2`.

### Spike de dérisquage (à séquencer en premier)
- Valider empiriquement **`getUserMedia` + AEC dans WKWebView (Tauri v2)** :
  `NSMicrophoneUsageDescription` + câblage du délégué de permission média. **DoD** :
  getUserMedia renvoie un stream ET l'AEC atténue la voix de Bob sous un seuil mesurable.
  **Fallback** si échec : capture + lecture + AEC en **Rust** (`cpal` +
  `webrtc-audio-processing`), webview = UI only. Le reste du PRD branche sur le résultat.

### Frontend
- **`MicCapture`** (AudioWorklet) — getUserMedia + echoCancellation, downsample 16 kHz,
  envoi binaire WS. Armé par le toggle voix.
- **`audioPlayer`** (existant) — réutilisé pour la sortie TTS ; le barge-in annule la
  lecture en cours.
- **`RolePicker`** — `SettingsControl` passe d'un switch global à une **section par-rôle**
  (provider Claude/LM Studio + base_url + modèle + context length par rôle) + une **section
  STT** à part (moteur whisper.cpp, modèle réglable). Indicateur d'état de floor dans le HUD.

### Latence (definition-of-done produit)
- barge-in **< 300 ms** ; endpoint→premier audio **< 800 ms** (Draft committé) / **< 1.5 s**
  (à froid) ; backchannel **< 500 ms**. Instrumenté via Debug View ; assertable par le
  harnais.

## Testing Decisions

Tests unitaires demandés sur **tous les modules** construits/modifiés (réponse A), **en
plus** du harnais d'attestation e2e.

**Ce qui fait un bon test** : il vérifie le **comportement externe** d'un module via son
interface publique, jamais ses détails d'implémentation. Pour les modules déterministes
(FSM, budget, endpoint, gate de commit, rétention, provider), on teste des entrées→sorties
exactes. Pour tout ce qui touche un LLM, on teste des **invariants/contrats** (un `say` a
été émis, l'état atteint, la latence sous cible) et non le texte généré, et on fake le LLM.

**Modules testés en isolation (pur / quasi-pur — priorité)** :
- `TurnFsm` — transitions exhaustives (dont barge-in et garde anti-overlap).
- `ModelBudget` — footprint + fit-check par host, dépassement, marges, host distant
  (override/skip).
- `Endpointer` — séquences VAD/silence + signal sémantique → décisions de fin-de-tour.
- `SpeculativeDraft` gate de commit — fast-path préfixe, garde de similarité, divergence.
- `ThinkerStateProvider` — projection du snapshot en `ContextEntry` (golden-prompt style).
- `VoiceRetentionPolicy` — éviction par âge + taille, caps séparés audio/texte.
- `RoleSelectionStore` — map par-rôle, migration depuis sélection globale, décodage
  défensif (fichier corrompu/partiel).
- `LMStudioManager` v2 — multi-load, offload sélectif ref-compté, refus sur budget
  (SDK faké au même boundary qu'aujourd'hui).

**Modules à interface, testés via fakes** :
- `SttEngine` — fixtures WAV → transcript attendu (moteur réel optionnel, marqué lent).
- `FakeLlmBackend`, `ScenarioRunner`, `EphemeralBackend` — testés sur des scénarios fixtures
  minimaux (un scénario qui passe, un qui échoue → exit non-zéro + verdict JSON correct).
- `BargeInController` — timeline simulée (parole injectée pendant `bob_speaking`).

**Tests e2e (harnais d'attestation)** : un scénario déclaratif par slice, lancé par
`bob attest` contre le backend éphémère + FakeLlmBackend, asserté sur invariants. C'est la
DoD de chaque issue.

**Prior art dans le repo** :
- `backend/tests/_harness` (golden-prompt) pour les providers/assembleur.
- Fake du SDK `lmstudio` au boundary (`tests/test_lm_studio_manager.py`) — modèle pour
  `FakeLlmBackend` et les tests `LMStudioManager` v2.
- Tests real-transport récents (intégration MCP) + scripts probe (`sdk_stream_probe.py`)
  comme base pour le drive-layer WS du harnais.
- `EventRetentionPolicy` comme prior art de `VoiceRetentionPolicy`.

## Out of Scope

- **Modèle speech-to-speech unifié** (PersonaPlex/Moshi) comme cerveau principal — explicitement
  écarté ; éventuel fast-path chitchat = pari futur séparé.
- **Backchannels en overlap** (par-dessus la parole) et l'état `overlap` du FSM — reportés
  (on reste sur backchannels dans les pauses).
- **Wake-word** et **push-to-talk** — reportés (mic always-on quand mode voix ON).
- **Classifieur d'endpoint sémantique dédié** (CamemBERT/DistilBERT fine-tuné) — non requis :
  le Thinker émet le signal. Reste une amélioration future possible.
- **Détection automatique de la RAM d'un host distant** — l'utilisateur règle le plafond ou
  on skip.
- **`--external` backend** pour le harnais (éphémère uniquement au départ).
- **Mobile / autres OS** — cible macOS Apple Silicon.
- **Annulation d'écho côté backend Python** — écartée au profit de l'AEC webview (ou Rust en
  fallback).

## Further Notes

- **Séquençage recommandé** : (1) **spike AEC/getUserMedia** + (2) **squelette du harnais**
  (drive-layer WS, FakeLlmBackend, ScenarioRunner, EphemeralBackend, mode `--text`, verdict
  JSON) en tout premier, pour rendre toutes les slices suivantes attestables. Le mode
  `--audio` du harnais arrive après le canal binaire WS + le `SttEngine`.
- **Réversion assumée** : le passage de l'offload-first au multi-load budget-aware revient en
  partie sur une décision robustesse antérieure (offload-first anti-OOM, 2026-06-05). Le
  garde-fou budget (`ModelBudget`) est ce qui rend cette réversion sûre — à traiter avec soin
  (les tests `test_lm_studio_manager.py` existants encodent l'ancien comportement et devront
  évoluer).
- **Conflits code à refactorer (pas du greenfield)** : `lm_studio_manager.load()`,
  `factory.py` (`_apply_selection` + builders), `llm_router` selection endpoints, le WS
  (ajout du canal binaire). À scoper comme issues de refacto avec migration + tests.
- **Tunables (réglés en route, valeurs par défaut dans le PRD)** : cadence de la boucle
  Thinker (par partiel vs intervalle debounced), seuils VAD/confirmation/endpoint, seuil de
  similarité du commit Draft, marges budget/rétention.
- **Contrainte cerveau réseau** : Claude CLI = fallback non-natif (pas de tokens précoces) ;
  pour un TTFT bas, les rôles latence-critiques (Speaker/Draft) devraient pointer un modèle
  LM Studio local. Le picker **expose** ce compromis, ne le tranche pas.
- **Dépendances nouvelles probables** : `whisper.cpp` (binding Python, ex. `pywhispercpp`),
  éventuellement `cpal` + `webrtc-audio-processing` (Rust) si le spike échoue.

---

# Annexes — Contrats & précision d'implémentation

> Objectif : zéro ambiguïté pour l'implémentation. Tout contrat ci-dessous est
> normatif. Les events transitent par `event_bus_v2.emit_event(payload, category=…)`
> et atterrissent dans le ring buffer `/ws/debug` (`DebugEvent`), où le harnais
> asserte. Nouvelle `DebugCategory = "voice"` pour tout le temps-réel vocal.

## A. Contrats d'événements WS

### A.1 Client → serveur
| Canal | Frame | Payload | Notes |
|---|---|---|---|
| binaire (NOUVEAU) | `bytes` | PCM 16 kHz mono s16le, ~20–40 ms | 1er octet = tag de type (`0x01` = mic frame) pour cohabiter avec d'futurs flux binaires ; le reste = samples. Séquence implicite par ordre d'arrivée. |
| JSON | `voice_start` | `{type, window, ts_client}` | mic armé (toggle ON) |
| JSON | `voice_stop` | `{type, ts_client}` | mic fermé (kill-switch) |
| JSON | `client_text` | `{type, text}` | input texte (path hybride existant, inchangé) |

### A.2 Serveur → client + ring buffer (`category:"voice"`)
Chaque event a `type` + `turn_id` (corrélation d'un tour vocal) + `ts` (monotone serveur).
| `type` | Payload | Émis quand |
|---|---|---|
| `stt_partial` | `{turn_id, text, stable_prefix_len, ts}` | chaque hypothèse partielle whisper |
| `stt_final` | `{turn_id, text, ts}` | transcript figé à l'endpoint |
| `turn_state` | `{turn_id, from, to, reason, ts}` | chaque transition FSM (voir B) |
| `thinker_snapshot` | `{turn_id, seq, corrected_text, variables, next_step_plan, user_turn_complete, ts}` | chaque mise à jour du `LiveTranscriptState` |
| `backchannel` | `{turn_id, token, ts}` | backchannel émis (pause) |
| `draft_status` | `{turn_id, state: drafting\|ready\|committed\|discarded, reason?, ts}` | cycle de vie du Draft spéculatif |
| `bargein` | `{turn_id, detected_ts, cut_ts, committed_spoken_text}` | barge-in confirmé + coupure |
| `speech_delta` | (existant, 0049) `{msg_id, delta}` | streaming say |
| `audio_chunk` / `audio_end` | (existants) | TTS PCM sortant |
| `turn_latency` | `{turn_id, marks: {...}, derived: {...}}` | fin de tour (voir F) |

> **Privacy** : `stt_*`, `thinker_snapshot`, `committed_spoken_text` portent du contenu
> utilisateur → ils passent en `payload` complet vers le client mais avec `debug_payload`
> **scrubbé** (texte tronqué/masqué) vers le ring buffer, sauf si la rétention debug est
> active (voir E.3). Réutilise le mécanisme `debug_payload` d'`emit_event`.

## B. Table de transitions `TurnFsm` (normative)

États : `idle`, `user_speaking`, `thinking`, `bob_speaking`. Actions entre `[]`.

| État courant | Événement | → État | Action |
|---|---|---|---|
| `idle` | `vad_speech_start` | `user_speaking` | `[start_turn(turn_id), start_thinker]` |
| `user_speaking` | `stt_partial` | `user_speaking` | `[feed_thinker, feed_draft]` |
| `user_speaking` | `vad_pause` (pause courte) | `user_speaking` | `[maybe_backchannel]` |
| `user_speaking` | `endpoint` (VAD silence OU `user_turn_complete`) | `thinking` | `[freeze_transcript, request_commit_or_generate]` |
| `thinking` | `draft_committed` | `bob_speaking` | `[speak(committed)]` |
| `thinking` | `draft_miss` / pas de draft | `bob_speaking` | `[speak(generate_cold)]` |
| `thinking` | `vad_speech_start` (user reprend) | `user_speaking` | `[cancel_generation, resume_thinker]` |
| `bob_speaking` | `bargein_confirmed` (VAD + fenêtre 200–300 ms) | `user_speaking` | `[cancel_llm_stream, cancel_tts, commit_spoken_partial, start_thinker]` |
| `bob_speaking` | `tts_end` | `idle` | `[finalize_turn, persist_transcript]` |
| `*` | `voice_stop` | `idle` | `[teardown_turn]` |

> Invariants assertables : jamais deux tours `turn_id` en `bob_speaking` simultanés ; tout
> `bargein_confirmed` ⇒ `cut_ts − detected_ts ≤ cible` ; tout `endpoint` en `thinking` ⇒ un
> `bob_speaking` ou un retour `user_speaking` suit (pas de blocage).

## C. Schéma des scénarios d'attestation (YAML) + verdict JSON

```yaml
# scenarios/<slice>.attest.yaml
name: bargein-cuts-within-300ms
description: Bob coupé en <300ms quand l'user reprend la parole
backend: ephemeral          # ephemeral | external
llm: fake                   # fake | real
fake_llm:                   # réponses scriptées (si llm: fake)
  - role: jarvis
    on_input_contains: "météo"
    reply: "Il fait beau aujourd'hui à Paris, vingt degrés et."
timeline:
  - at_ms: 0     ; do: inject_text   ; text: "quel temps à Paris"
  - at_ms: 0     ; do: wait_state    ; state: bob_speaking ; timeout_ms: 1500
  - at_ms: +200  ; do: inject_audio  ; fixture: fixtures/reprise.wav   # ou inject_text
  - do: wait_event ; type: bargein ; timeout_ms: 800
assertions:
  - kind: fsm_reached         ; state: user_speaking
  - kind: bargein_within_ms   ; max: 300
  - kind: role_used_model     ; role: jarvis ; model: fake-jarvis
  - kind: committed_equals_spoken
  - kind: no_error_events
```

**Vocabulaire d'assertions** (extensible) : `fsm_reached`, `event_emitted`,
`latency_lt_ms` (mark→mark), `bargein_within_ms`, `role_used_model`,
`committed_equals_spoken`, `deliverable_nonempty`, `audio_chunks_gte`,
`stt_final_matches` (regex/contains, mode audio), `transcript_roundtrip_similarity_gte`
(`--deep`), `no_error_events`.

**Verdict JSON** (stdout) :
```json
{
  "scenario": "bargein-cuts-within-300ms",
  "ok": false,
  "duration_ms": 1840,
  "assertions": [
    {"kind": "bargein_within_ms", "ok": false, "expected_max": 300, "actual": 412}
  ],
  "events_captured": 37,
  "backend": {"mode": "ephemeral", "port": 53122},
  "llm": "fake"
}
```
Exit code : `0` si `ok:true`, `1` sinon (gate CI / verify).

## D. Schéma de sélection par-rôle (fichier JSON, décodage défensif)

`{BOB_DATA_DIR}/llm_selection.json` évolue (PAS une migration SQL — c'est un fichier JSON) :
```json
{
  "schema_version": 2,
  "roles": {
    "jarvis":   {"provider": "lm_studio", "base_url": "http://localhost:1234/v1", "lm_model": "qwen2.5-7b-instruct", "context_length": {"qwen2.5-7b-instruct": 16384}},
    "thinker":  {"provider": "lm_studio", "base_url": "http://localhost:1234/v1", "lm_model": "qwen2.5-3b-instruct", "context_length": {}},
    "draft":    {"provider": "lm_studio", "base_url": "http://localhost:1234/v1", "lm_model": "qwen2.5-1.5b-instruct", "context_length": {}},
    "subagent": {"provider": "claude_cli", "base_url": null, "lm_model": null, "context_length": {}}
  },
  "stt": {"engine": "whisper_cpp", "model": "large-v3-turbo"},
  "budget": {"ceiling_gib": null, "reserve_gib": 8, "per_host_override": {}}
}
```
**Migration `schema_version` 1→2** : l'ancien shape plat (`{provider,lm_model,context_length,base_url}`)
seed **les 4 rôles** avec la même valeur ; `stt`/`budget` prennent les défauts. Décodage
défensif conservé (clés manquantes/typées faux → défauts ; `ceiling_gib:null` ⇒ détecté).

## E. Modèle de données & migrations SQL

### E.1 `0010_voice_turns.sql`
```sql
CREATE TABLE voice_turns (
  turn_id      TEXT PRIMARY KEY,
  jarvis_msg_id TEXT,                 -- lien vers jarvis_messages quand committé
  final_transcript TEXT,
  spoken_text  TEXT,                  -- ce que Bob a réellement dit (post-barge-in)
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  end_reason   TEXT,                  -- 'completed' | 'bargein' | 'voice_stop' | 'error'
  draft_outcome TEXT,                 -- 'committed' | 'discarded' | 'none'
  latency_json TEXT                   -- marks + derived (voir F)
);
```
### E.2 `0011_voice_audio_blobs.sql`
```sql
CREATE TABLE voice_audio_blobs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id    TEXT NOT NULL,
  kind       TEXT NOT NULL,           -- 'mic_in' | 'tts_out'
  path       TEXT NOT NULL,           -- fichier WAV sur disque (pas en DB)
  bytes      INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
```
### E.3 Rétention (`VoiceRetentionPolicy`)
Purge automatique, **caps séparés** : `voice_audio_blobs` borné par taille (défaut 1.5 Gio,
le plus vieux d'abord, supprime fichier + ligne) ; `voice_turns` borné par âge (défaut 30 j).
Réglable en settings. Réutilise l'esprit `EventRetentionPolicy` (bornes octets + âge).

## F. Instrumentation de latence (normative)

Marks (ms, horloge monotone serveur) par `turn_id`, émis dans `turn_latency.marks` :
`t_first_mic_frame`, `t_first_partial`, `t_endpoint`, `t_draft_ready`, `t_commit_decision`,
`t_first_audio_chunk`, `t_tts_end`, et sur barge-in `t_bargein_detected`, `t_cut`.
Dérivés (`turn_latency.derived`) :
- `endpoint_to_first_audio_ms = t_first_audio_chunk − t_endpoint` (cible <800 committé / <1500 froid)
- `bargein_cut_ms = t_cut − t_bargein_detected` (cible <300)
- `backchannel_ms` (cible <500)
- `draft_hit` (bool : committé vs froid)
C'est la source des assertions `latency_lt_ms` / `bargein_within_ms`.

## G. Matrice d'erreurs / dégradation (anti « à moitié »)

| Panne | Détection | Comportement | Surface |
|---|---|---|---|
| Modèle whisper absent | au boot/1er usage | download lazy (comme Kokoro) + toast `tts_preparing`-like | event `voice` severity warn |
| STT échoue en cours | exception engine | tour avorté proprement (`end_reason:error`), retour `idle`, toast ; pas de crash | event severity error |
| OOM au load (budget OK mais réel KO) | `LMStudioLoadError` | garder l'état précédent, refuser le swap de ce rôle, avertir ; **jamais** laisser 0 modèle pour un rôle actif | router 4xx + event |
| Budget dépassé (check) | `ModelBudget` | refuser AVANT load + message « dépasse le plafond, libère un rôle » | router 4xx |
| Host distant injoignable | `LMStudioUnavailableError` | ce rôle marqué offline dans le picker ; fallback configurable (rien d'auto) | ping + event |
| WS binaire coupé en plein tour | socket close | finalize le tour (`voice_stop` implicite), persiste le partiel | event |
| AEC échoue runtime (webview) | écho détecté / spike négatif | dégrade en **half-duplex gate** (mute mic pendant `bob_speaking`) comme filet ; flag visible | event warn |
| Draft model indispo | build client KO | désactive l'anticipation (toujours froid), le reste marche | event warn |
| Barge-in faux positif | — | toléré par la fenêtre de confirmation ; pas de coupure si <fenêtre | — |

> Règle générale : **toute panne dégrade vers un mode fonctionnel inférieur connu**
> (half-duplex, froid, texte) plutôt que de casser le tour. Chaque ligne = un cas
> de test (fake + scénario).

## H. Cadence & cancellation du `ThinkerLoop`

- Re-déclenche sur **nouveau `stt_partial`** mais **debounced** (défaut 250 ms ; réglable) —
  jamais plus d'une inférence Thinker en vol par tour.
- Chaque snapshot porte un `seq` croissant ; un snapshot `seq` < dernier vu est **ignoré**
  (anti stale).
- **Annulation coopérative** sur `endpoint` (on fige), `bargein`, `voice_stop` (sous le
  `TaskGroup` existant des sous-agents : cancel + grâce + hard-kill).
- Le `user_turn_complete` du snapshot ne déclenche l'endpoint que **confirmé par 1 partiel
  suivant stable** (anti faux-positif), sinon le VAD silence reste le filet.

## I. Critères d'acceptation du spike AEC (gate)

PASS ssi, dans le webview Tauri v2 cible : (1) `getUserMedia({audio:{echoCancellation:true}})`
renvoie un `MediaStream` actif après ajout de `NSMicrophoneUsageDescription` + câblage du
délégué de permission ; (2) en jouant un TTS connu pendant la capture, l'énergie de l'écho
dans le micro est atténuée **≥ 25 dB** (mesuré par un test fixture) ; (3) un mot prononcé
par-dessus le TTS est transcrit correctement. ÉCHEC d'un critère ⇒ bascule **fallback Rust**
(`cpal` + `webrtc-audio-processing`) documentée, et les issues aval re-pointent leur source
de capture. Le spike est la **1re issue bloquante**.

## J. Séquence de boot & (re)chargement des modèles

1. Lire `llm_selection.json` (v2) → map des 4 rôles.
2. Grouper les rôles `lm_studio` **par host** (`base_url`).
3. Par host : calculer le plafond (local détecté − reserve ; distant : override ou skip),
   sommer les footprints requis (`ModelBudget`), **refuser+avertir** si dépassement.
4. Charger les modèles manquants (offload sélectif ref-compté : n'évince que les modèles
   non référencés sur ce host) ; rôles `claude_cli` = pas de load.
5. Marquer chaque rôle `ready` / `offline` ; exposer l'état au picker.
6. **Reassignment** (picker) : recompute le ref-count, charge la cible si besoin (re-check
   budget), évince l'ancien modèle s'il n'est plus référencé, rebuild **uniquement** le
   client du rôle changé.
