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

**Décisions ouvertes qui restent (communes, cf. Blockers) :** snapshot live vs
contexte par-tour ; draft spéculatif vs codec tool-call/validation ; baseline
latence + métriques (taux commit/jet, compute gaspillé).

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
