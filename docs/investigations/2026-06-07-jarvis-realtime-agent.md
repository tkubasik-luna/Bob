# Investigation — Rendre Jarvis « temps réel »

**Date :** 2026-06-07
**Statut :** investigation seule, aucun code écrit.
**Déclencheur :** NVIDIA PersonaPlex (https://github.com/NVIDIA/personaplex), agent
vocal full-duplex temps réel. Question : comment transformer Jarvis en agent plus
temps réel ? Piste utilisateur : « combinaison d'agents / session qui analyse la
pensée et parle en parallèle / anticiper la réponse ».

---

## TL;DR

- **Bob aujourd'hui = texte-entrée, voix-sortie, tour-par-tour.** Aucun STT / micro
  / VAD dans le code (vérifié par grep — tous les hits `audio` sont en *sortie* :
  player Kokoro, audio-level de la sphère). L'utilisateur tape ; Kokoro répond en
  voix, granularité phrase. C'est le **gap #1** pour le temps réel.
- **PersonaPlex n'est PAS un système multi-agent** — c'est un **modèle unique
  speech-to-speech 7B** (architecture Moshi : codec Mimi + LLM Helium, un seul
  réseau qui prédit texte+audio en autoregressif, transformer dual-stream). Son
  temps réel est *dans les poids*, sur GPU. Impossible à répliquer avec un cascade
  Claude-CLI / LM-Studio. L'adopter tel quel = remplacer le cerveau de Bob (outils,
  raisonnement Claude, Jarvis) par une bouche 7B bavarde. **Mauvais fit en voie
  principale.**
- **L'instinct utilisateur (combinaison d'agents + pensée parallèle + anticipation)
  est l'architecture OPPOSÉE à PersonaPlex — et la BONNE pour Bob.** Elle colle à
  la ligne de recherche cascade-parallèle. On garde le cerveau Claude, on ajoute le
  comportement temps réel par-dessus.

---

## Ce qu'est vraiment PersonaPlex

| Aspect | Détail |
|---|---|
| Type | Modèle unique S2S 7B, **pas** une orchestration d'agents |
| Base | Moshi — codec **Mimi** (ConvNet+Transformer, encode/décode l'audio en tokens) + LLM **Helium** |
| Flux | 3 streams : audio user (entrée), texte agent, audio agent (sortie) |
| Full-duplex | Transformer dual-stream → écoute + parle en même temps, gère interruptions / chevauchements |
| Latence | TTFT ~170 ms, réponse-interruption 240 ms, turn-taking 90.8 %, task-adherence 4.29 |
| Persona | 3 niveaux de prompt : voix (timbre/prosodie via échantillons audio), texte (rôle/scénario), system (règles/objectifs) |
| Licence / matériel | MIT ; tourne sur GPU (exigences précises non documentées dans les sources lues) |

Conclusion : excellent pour la *bouche* (chitchat vocal fluide), nul pour le
*cerveau* (pas d'outils, pas de raisonnement Claude). À considérer seulement comme
fast-path optionnel (voir Phase 5).

---

## Les 3 idées → techniques de recherche → réutilisation Bob

| Idée utilisateur | Recherche | Mécanisme | Réutilisation Bob |
|---|---|---|---|
| « combinaison d'agents » | **LTS-VoiceAgent** (Listen-Think-Speak) | **Thinker** de fond maintient un snapshot d'état JSON ; **Speaker** de premier plan génère à partir du plan du tour précédent ; **Orchestrator** fait le handoff async (paradigme Restate-Consult-Solve) | Orchestrateur + TaskGroup sous-agents existent déjà — recadrer, pas reconstruire |
| « analyser la pensée, parler en parallèle » | **Inner Thoughts** | Réservoir de pensées async sur le transcript live ; 8 heuristiques scorent chaque pensée ; seuil when-to-speak + décroissance-silence λ=1.02 | Étend la file proactive de Bob (déjà gate sur thinking/typing) |
| « anticiper la réponse » | **RelayS2S** dual-path spéculatif | Petit modèle rapide brouillonne la réponse sur l'entrée *partielle* ; vérifie vs transcript final ; commit les tokens alignés, jette le reste | Bob fait déjà 2 appels LLM/tour (décision-spawn + réponse) — rendre le 2e spéculatif |

**Glu de déclenchement (LTS-VoiceAgent) :** un DistilBERT léger score chaque préfixe
ASR, déclenche le LLM seulement quand `P_trigger > 0.65` (clause complète), supprime
les « euh… ». Remplace le VAD bête. Mesuré : taux d'interruption 5–10 % vs 41–90 %
pour les baselines VAD ; TTFT 207–236 ms ; time-to-first-sentence 332–415 ms.

---

## Système final — architecture cible

Boucle continue, 3 rôles parallèles, anticipation, proactivité :

```
   MICRO ──► [ÉCOUTE]  STT streaming + détection fin-de-phrase sémantique
                 │  (transcript partiel qui grandit mot par mot)
                 ▼
            [PENSÉE]  agent de fond, modèle léger local
                 │     met à jour en continu un État :
                 │     { texte_corrigé, variables, plan_prochaine_étape }
                 │     ┌──────────────────────────────────────┐
                 │     │ RÉSERVOIR pensées (Inner Thoughts) :  │
                 │     │ génère idées en //, score chacune,    │
                 │     │ décide SI/QUAND parler (seuil + temps)│
                 │     └──────────────────────────────────────┘
                 ▼
            [ANTICIPATION]  brouillon de réponse AVANT la fin de phrase
                 │           (petit modèle rapide, sur transcript partiel)
                 ▼
            [PAROLE]  Jarvis say-path, lit l'État + le brouillon
                 │     vérifie vs transcript final → garde ou jette
                 ▼
   KOKORO ◄── audio streaming (déjà là)
```

- **ÉCOUTE (Listen)** — *nouveau, prérequis.* STT streaming local (faster-whisper),
  endpoint sémantique (pas basé silence). Produit le transcript partiel croissant.
- **PENSÉE (Think)** — sous-agent de fond, modèle léger. Maintient un
  `StateSnapshot{ texte_corrigé, variables, plan_prochaine_étape }`, MAJ par mot.
  Survit aux changements d'intention en milieu de phrase (l'état reste, seul le
  brouillon est jeté). Nouveau provider de contexte live. Réutilise le runtime
  sous-agent TaskGroup.
- **PAROLE (Speak)** — le `say` actuel de Jarvis, mais **consulte le snapshot Pensée**
  au lieu de partir à froid.
- **ANTICIPATION** — petit modèle brouillonne la réponse sur transcript partiel avant
  l'endpoint ; au transcript final, vérifie → commit (latence quasi-nulle) ou jette.
- **PROACTIVITÉ (Inner Thoughts)** — réservoir async + when-to-speak + décroissance-
  silence → backchannels (« mm-hm », « ok je vois ») + interventions spontanées.

### Ressenti utilisateur

- Bob t'écoute *pendant* que tu parles, comprend avant la fin.
- Réponse immédiate quand tu t'arrêtes (déjà brouillonnée).
- Bob peut réagir / interrompre au lieu d'attendre poliment.
- Sensation : interlocuteur vivant, pas distributeur question→réponse.

### Réutilisé vs neuf

| Brique | État |
|---|---|
| Orchestrateur, sous-agents TaskGroup, file proactive | **déjà là** — recadrer |
| Kokoro TTS streaming, sphère HUD | **déjà là** |
| Cerveau Jarvis + outils + contexte borné | **déjà là** |
| ÉCOUTE (STT streaming + endpoint sémantique) | **neuf — prérequis #1** |
| Snapshot État (provider live) | neuf |
| Brouillon spéculatif | neuf |
| Streaming token (`stream_complete`/`stream_chat`) | **déjà là** (issue 0049) ; Claude CLI = fallback non-natif |
| Streaming du **contenu** de saisie (pas juste `client_typing` bool) | **neuf — prérequis du léger** |

---

## Blockers (barre robustesse)

1. **Pas de boucle audio entrée.** Prérequis du full-duplex. + **annulation d'écho**
   (le TTS Kokoro de Bob bave dans le micro) — dur, non-optionnel pour le barge-in.
2. **Streaming token : déjà là pour LM Studio, fallback pour Claude CLI.**
   ⚠️ *Correction 2026-06-07 :* le streaming token EXISTE déjà —
   `LLMClient.stream_complete` / `stream_chat` → `AsyncIterator[StreamChunk]`
   (issue 0049 / PRD 0006). La note `0002-voice-mode.md:24` (« no deltas ») est
   **périmée**. Nuance : LM Studio = streaming natif (`stream=True`,
   `delta.reasoning_content`) ; Claude CLI = **fallback** qui rejoue la réponse
   complète en chunks (pas de vrais tokens précoces). ⇒ le brouillon spéculatif et
   le TTFT bas doivent tourner sur **LM Studio local**, pas Claude CLI.
3. **Réalité latence locale.** Les chiffres 170–240 ms viennent de modèles S2S GPU
   dédiés. Un Speaker 7B local sous LM-Studio n'y arrivera pas ; Claude CLI ajoute
   le RTT réseau. L'anticipation *masque* la latence, ne fabrique pas de GPU.
4. **Taxe tool-call + validate/retry.** Le « chaque émission est un tool call » de Bob
   + validation/retry Pydantic ajoute des tours qui combattent le temps réel. Le
   chemin spéculatif devra sans doute by-passer la validation lourde pour les
   brouillons, et ne valider qu'au commit.
5. **Compute gaspillé.** Les brouillons jetés brûlent du GPU/CPU local — il faut un
   design cancel-cheap + un petit modèle de brouillon dédié (pas le modèle principal).

---

## Chemin par phases

- **Phase 0 — Listen.** STT streaming + VAD + endpoint sémantique. Le plus gros lift.
- **Phase 1 — Split Think/Speak.** Thinker de fond → provider StateSnapshot ; Speaker
  le consulte. *C'est* la « combinaison d'agents ».
- **Phase 2 — Signal d'entrée partiel + plomberie backends.** (Le streaming token
  est déjà là, cf. blocker #2 — pas de refonte client.) En mode léger : streamer le
  buffer de saisie en cours (debounced) vers le backend — aujourd'hui Bob n'envoie
  qu'un `client_typing` *booléen*, pas le contenu. + câbler des backends par-rôle
  THINKER / DRAFT (le mécanisme `JARVIS_BACKEND` / `SUBAGENT_BACKEND` existe déjà).
- **Phase 3 — Anticipation.** Brouillon spéculatif sur partiel, verify-commit. Petit
  modèle de brouillon (les backends par-rôle existent déjà).
- **Phase 4 — Proactivité Inner-Thoughts.** Réservoir + when-to-speak + backchannels.
- **Phase 5 (pari séparé) — Bouche hybride.** PersonaPlex/Moshi en fast-path pour le
  chitchat seul ; cascade Jarvis pour le travail outillé. S2S = bouche, Jarvis =
  cerveau. Gros pari, perd le raisonnement Claude dans la voie S2S. Évaluer, ne pas
  s'engager.

---

## Le fork (décision utilisateur — change tout le scope)

> **🟢 DÉCIDÉ 2026-06-07 : Full-duplex direct.** Micro toujours ouvert + STT
> streaming + barge-in + annulation d'écho dès le départ. À la PersonaPlex.
> **AEC = WebRTC réel** (vrai barge-in). **Cerveau = sélectionnable par-rôle dans
> les réglages** (évolution UI du picker 0013), pas de backend figé. Voir « Gaps
> spécifiques full-duplex » ci-dessous pour le scope réel à lancer.

- **Final léger (RECOMMANDÉ pour démarrer)** — reste texte-in / voix-out. Ajoute
  Pensée + Anticipation + Proactivité. Bob anticipe et paraît vivant, l'utilisateur
  tape encore. Prérequis quasi-nul, gain rapide, réutilise toute la machinerie Bob.
  = Phases 1→4, skip Phase 0 (STT).
- **Final full-duplex** — micro toujours ouvert, barge-in, parole qui se chevauche.
  À la PersonaPlex. Exige Phase 0 (STT) + annulation d'écho. Lourd.

Même cible : la version légère est juste le full-duplex **moins la couche audio-entrée**.
Livrer le léger d'abord, ajouter le micro après sans rien jeter.

---

## Gaps spécifiques full-duplex (décidé 2026-06-07)

**Audio-entrée — tout greenfield, rien n'existe :**
- Capture micro (frontend Tauri / Web `getUserMedia`) → flux PCM vers backend ;
  nouveau contrat WS **client→serveur** (frames audio).
- STT streaming backend (faster-whisper / whisper-streaming, local) — choix du
  modèle + budget GPU/CPU.
- VAD + endpoint sémantique (trigger type DistilBERT `P>0.65`, ou VAD simple d'abord).

**Les deux sous-systèmes durs :**
- **Annulation d'écho (AEC). 🟢 DÉCIDÉ : AEC réel (WebRTC).** Micro ouvert → le TTS
  Kokoro de Bob rentre dans le micro → Bob s'entend et boucle. AEC logicielle WebRTC
  → vrai barge-in (Bob coupable à la voix *pendant* qu'il parle). Sous-système le
  plus dur ; tuning à prévoir.
- **Barge-in mid-génération.** Interrompre depuis la parole *détectée* pendant que
  Bob parle : annuler le stream LLM + TTS + committer le partiel. Bob coupe déjà le
  TTS sur nouveau message *texte*, mais pas depuis l'audio en cours.

**Orchestration floor :**
- **FSM turn-taking** (style FlexDuo) : idle / user-parle / bob-parle / overlap /
  backchannel. Neuf.

**Cerveau par-rôle, sélectionnable en réglages — 🟢 DÉCIDÉ :**
- Pas de backend figé. Chaque *niveau / rôle* (cerveau Jarvis, Thinker, Speaker,
  Draft spéculatif) est **assignable indépendamment** à n'importe quel backend
  (Claude CLI / modèle LM Studio local) depuis les **réglages**. ⇒ **évolution UI**
  du picker existant (feature 0013 `SettingsControl`, aujourd'hui un switch global)
  vers un sélecteur **par-rôle**.
- Contrainte rappelée : Claude CLI = fallback non-natif (pas de tokens précoces) →
  pour un TTFT bas, choisir un modèle LM Studio local sur les rôles latence-critiques
  (Speaker / Draft). Le réglage **expose** le compromis, ne le tranche pas.

**Matériel :**
- STT + Thinker/Draft + Speaker + Kokoro TTS en concurrence locale. Cible hardware
  (GPU / VRAM) à fixer.

**Décisions ouvertes qui restent** → ✅ résolues par le grill du 2026-06-07, voir
« Design verrouillé — grill 2026-06-07 » ci-dessous.

---

## Design verrouillé — grill 2026-06-07

Scope : **tout d'un coup** (audio I/O + Thinker + Draft + picker par-rôle ensemble).

### Pipeline & placement
- **STT** : backend. Frontend capture micro + **AEC WebRTC** (`getUserMedia`
  `echoCancellation:true`) → frames **binaires PCM 16kHz mono s16le** (~20-40ms) sur
  un nouveau canal WS client→serveur.
- **Moteur STT** : **whisper.cpp** (Metal/CoreML), modèle défaut **large-v3-turbo**
  (settable). faster-whisper écarté (CTranslate2 = CPU-only sur Mac).
- **Matériel** : Mac Apple Silicon **32 Go+** unifiés.

### Orchestration
- **FSM turn-taking compact** : `idle / user_speaking / thinking / bob_speaking` +
  transition **barge-in**. Backchannel = action, pas un état (pas d'état overlap).
- **Endpointing** : hybride **VAD silence (~500-700ms) + endpoint sémantique**
  (classifieur léger : déclenche tôt sur clause complète / retient si inachevé).
- **Barge-in** : déclenché par **VAD + fenêtre de confirmation ~200-300ms** (filtre
  backchannels/bruits courts). Action : annuler stream LLM + TTS, **committer le
  texte déjà prononcé** dans l'historique, → `user_speaking`.
- **Backchannels** : légers ("mm", "ok"), **dans les pauses** (pas en overlap),
  gated par le Thinker + seuil de proactivité (Inner Thoughts when-to-speak).

### Penser en parallèle
- **Concurrence** : **modèles locaux distincts** (Thinker & Draft = mini 1-3B,
  Speaker = 7-8B) pour une vraie concurrence — un seul moteur d'inférence sérialise.
  Un rôle peut aussi être Claude (réseau, concurrence gratuite).
- **Thinker** : boucle de fond → écrit un **`LiveTranscriptState`** en mémoire ; un
  **provider pur `ThinkerStateProvider`** lit le dernier snapshot à l'assemblage
  (pattern `StateBlockProvider`→`TaskStore`). Assemblage reste pur, déclenché par
  l'endpoint du FSM.
- **Draft spéculatif** : **texte brut hors codec** sur le transcript partiel ; à
  l'endpoint, si aligné → adopté dans le say-path normal → TTS, sinon jeté. Ne
  spécule **que la réponse conversationnelle**, pas les tours qui dispatchent un
  outil. Commit recommandé : fast-path préfixe + garde de similarité sémantique.

### Picker par-rôle (évolution feature 0013)
- **Map complète par-rôle** : `{role: LLMSelection}` pour
  `speaker (jarvis) / thinker / draft / subagent`. **Chaque rôle choisit Claude CLI
  *ou* LM Studio** + modèle (pas juste un modèle LM). `LLMSelection` porte déjà
  `provider` → réutilisé. Migration : la sélection globale actuelle initialise chaque
  rôle. UI : `SettingsControl` passe d'un switch global à une section par-rôle.

### Contraintes & cibles
- **Cerveau réseau (Claude)** = pas de tokens précoces → pour un TTFT bas, mettre un
  modèle **LM Studio local** sur les rôles latence-critiques (Speaker/Draft). Le
  picker **expose** le compromis, ne le tranche pas.
- **Latence (DoD)** : barge-in **<300ms**, endpoint→premier audio **<800ms** (Draft
  committé) / **<1.5s** (à froid), backchannel **<500ms**. Instrumenté via Debug View.
- **Mic** : **always-on quand le mode voix est ON** (réutilise le toggle existant =
  kill-switch privacy).
- **Rétention** : persister transcripts + partiels + audio (Debug/replay/tuning) →
  **ajouter une retention policy bornée** (âge/taille), façon `EventRetentionPolicy`.

### Reste à trancher — ✅ résolu (grill 2026-06-07, 2e passe)
- **Endpoint sémantique** : pas de classifieur dédié → le **Thinker émet
  `user_turn_complete`** dans son snapshot ; VAD silence = filet robuste. (Le Thinker
  fait triple emploi : snapshot d'état + signal d'endpoint + trigger backchannel.)
- **Multi-modèles LM Studio** : faisable (SDK = multi-instances `load_new_instance`
  + `list_loaded_models` pluriel + inférence routée par `model`), **MAIS ⚠️ conflit
  code** — `LMStudioManager.load()` est **offload-first** (`lm_studio_manager.py:308` :
  évince tous les autres modèles avant de charger). Décision : **multi-load
  budget-aware + offload sélectif** (check budget RAM ≤ plafond − marge ; n'évince que
  les modèles non assignés à un rôle). + **factory par-rôle**
  (`build_thinker_client` / `build_draft_client`, chacun épingle son `model` ;
  `LMStudioClient` route par `model` ; `factory.py` ne gère qu'un `LLM_MODEL` global
  aujourd'hui).
- **Commit Draft** : **fast-path préfixe** (commit instantané, cas commun avec
  l'endpoint sémantique) + **garde de similarité légère** (token-overlap/embedding) sur
  divergence (slow-path, on regénère de toute façon).
- **Rétention** : **âge + taille** façon `EventRetentionPolicy`, caps séparés audio
  (~1-2 Go) vs transcripts (~30j), purge auto.

### ⚠️ Conflits code identifiés (à intégrer au PRD)
- `LMStudioManager.load()` offload-first (`lm_studio_manager.py:308`) → réécrire en
  multi-load budget-aware + offload sélectif (revient en partie sur la décision
  robustesse du 2026-06-05, voir mémoire `bob-llm-picker-robustness`).
- `factory.py` (`_apply_selection` + `build_*_client`) ne porte qu'**une** sélection
  globale → étendre en map par-rôle (cf. picker Q10/Q14).
- WS de Bob = JSON texte only → ajouter un **canal binaire** (frames PCM entrants).

### Conflits — sous-décisions tranchées (grill 2e passe, 2026-06-07)

**#3 Audio / AEC (le plus risqué) :**
- **Webview-natif + spike de validation d'abord.** `getUserMedia({audio:{echoCancellation:true}})`
  dans le webview → WKWebView annule sa propre sortie (capture + lecture restent en
  webview). **Spike gate early** : ajouter `NSMicrophoneUsageDescription` + vérifier le
  délégué de permission média Tauri v2. **DoD spike** : getUserMedia renvoie un stream
  ET l'AEC atténue la voix de Bob sous un seuil mesurable. **Fallback** si échec :
  Rust-natif (`cpal` + `webrtc-audio-processing`, capture + lecture + AEC en Rust,
  webview = UI). Le PRD **branche sur l'issue de spike**.
- Canal **WS binaire** entrant (frames PCM 16k) dans `ws_router` ; capture via
  AudioWorklet (downsample 16k). Mic possédé par la fenêtre HUD `new`.

**#1 `LMStudioManager` offload-first → multi-load budget-aware :**
- **Footprint** = taille fichier disque (GGUF/MLX) + marge KV-cache (∝ `context_length`).
- **Plafond** = RAM détectée (sysctl) − marge OS (~8 Go), **override + marge réglables**
  en settings.
- **Per-host** : manager + budget **par host** (conséquence du base_url par-rôle). Host
  local → plafond détecté ; **host distant → override settings, sinon skip** (try+catch OOM).
- **Offload sélectif ref-compté** : un modèle n'est évincé que quand **aucun rôle ne le
  référence** (sur le même host). Fin de l'offload-first.

**#2 `factory.py` une sélection → map par-rôle :**
- **4 rôles** : `jarvis(=speaker)` / `thinker` / `draft` / `subagent`. STT = section
  settings à part (pas un rôle LLM).
- Sélection par-rôle = `{provider (claude_cli|lm_studio), base_url, lm_model,
  context_length}`. **base_url par-rôle complet** (chaque rôle son serveur → Thinker
  local + Speaker GPU distant possible).
- `build_<role>_client` épingle provider/base_url/model ; **`LMStudioClient` route par
  param `model`** ; `llm_swap` rebuild **uniquement le rôle changé**. Migration : la
  sélection globale actuelle seed les 4 rôles.

**Scope :** input texte **conservé** (hybride voix+texte additif, zéro régression).

**Tunables (PRD, non-bloquants) :** cadence boucle Thinker (par partiel vs intervalle
debounced) ; seuils VAD/confirmation/endpoint ; seuil similarité commit Draft ; marges
budget/rétention.

---

## Harnais d'attestation agent (`bob` CLI) — grill 2026-06-07

**Besoin (user)** : une mécanique pour que l'agent (Claude) **teste en autonomie
réelle** et atteste que chaque slice marche, au fur et à mesure. Contrainte :
full-duplex = hardware/humain (micro, audio temps réel) → l'agent ne peut pas
parler/entendre → injection de fixtures + assertions machine-lisibles.

**Forme** : un **`bob` CLI** (greenfield — aucun CLI aujourd'hui ; console_script via
`pyproject`, sous-commandes, sortie JSON, exit non-zéro). Driver headless + asserteur
par-dessus le spine d'events existant (`/ws/debug`, `event_bus_v2`).

- **Drive layer** : **black-box sur le vrai WS/HTTP** ; asserte sur le flux
  `/ws/debug`. Teste la vraie pile (transport inclus).
- **Injection entrée** : 2 modes — **`--audio fixture.wav`** (frames micro → STT →
  e2e, dépend du canal binaire WS) et **`--text`** (inject transcript, skip whisper →
  rapide + déterministe). Texte au quotidien, audio pour valider STT/endpoint.
- **Assertions** : **invariants/contrats**, pas le texte exact — FSM a atteint l'état
  X, `say` émis, latence < cible, **barge-in coupé <300ms**, rôle R a servi modèle M,
  deliverable non-vide, **texte committé == texte prononcé**. temp=0 où l'exactitude
  compte.
- **Validation TTS** (l'agent n'entend pas) : **texte pré-synthèse + count/octets/timing
  des `audio_chunk`** (couvre le barge-in = chunks coupés). Option **`--deep`** :
  round-trip TTS→whisper→compare (intelligibilité réelle, lent/non-déterministe).
- **Scénarios** : **déclaratifs YAML/JSON**, timeline d'événements timés (`inject à
  t=X`, `attendre état Y`) + assertions ; **verdict JSON** ; hook Python en
  échappatoire. **Un scénario par slice**, versionné.
- **Intégration build** : **chaque issue livre son scénario**, DoD =
  `bob attest <scenario>` au vert. L'agent lance la suite après chaque slice + en CI
  → atteste l'empilement au fur et à mesure, régressions vues tôt. Se branche sur
  l'adversarial-verify d'`implement-feature-v2`.
- **Backend** : **éphémère auto-géré + isolé** (BOB_DATA_DIR temp, DB fraîche, port
  dédié) ; boot→run→teardown en un ordre. Zéro pollution de l'état réel. `--external`
  plus tard.
- **LLM** : **fake scriptable par défaut** (déterministe, offline, CI) ; **`--real`**
  opt-in pour l'e2e vrai. Précédent : Bob fake déjà le SDK LM Studio en test.

**Conséquences PRD :**
- Nouveau **fake LLM backend** scriptable (via le switch provider de `factory.py`, ou
  injection de test) — réutilise le pattern de fake SDK existant.
- Le mode `--audio` **dépend du canal binaire WS** (conflit #3) + STT → atterrit après ;
  le **squelette harnais** (mode text, black-box WS, fake LLM, runner de scénarios,
  verdict JSON) peut être la **1re issue** (foundation) → tout le reste devient
  attestable dès le départ.
- Rend la feature **auto-vérifiante** : chaque slice ship + son attestation.

---

## Sources

- **PersonaPlex** — [GitHub](https://github.com/NVIDIA/personaplex) ·
  [preprint](https://research.nvidia.com/labs/adlr/files/personaplex/personaplex_preprint.pdf) ·
  [release notes](https://comfyui-wiki.com/en/news/2026-01-20-nvidia-personaplex-7b-v1-release)
- **LTS-VoiceAgent (Listen-Think-Speak)** — [arXiv 2601.19952](https://arxiv.org/html/2601.19952v1)
- **Inner Thoughts (pensée parallèle proactive)** — [arXiv 2501.00383](https://arxiv.org/html/2501.00383v2)
- **RelayS2S (dual-path spéculatif)** — [arXiv 2603.23346](https://arxiv.org/pdf/2603.23346)
- **The Silent Thought (raisonnement latent full-duplex)** — [arXiv 2603.17837](https://arxiv.org/pdf/2603.17837)
- **Revue S2S 2026** — [ksopyla.com](https://ai.ksopyla.com/posts/voice-to-voice-models-2026-review/)
- **Interne Bob** — [0002 Voice Mode](../features/0002-voice-mode.md) ·
  [0003 Jarvis Orchestrator](../features/0003-jarvis-orchestrator.md) ·
  [0007 Jarvis v2](../features/0007-jarvis-v2-context-overhaul.md)
