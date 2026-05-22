# PRD 0002 — Voice Mode (Bob parle)

## Problem Statement

Aujourd'hui Bob répond uniquement en texte dans la fenêtre de chat Tauri. L'utilisateur doit lire chaque réponse à l'écran, ce qui interdit tout usage "mains libres" / "yeux libres" (cuisine, conduite passager, déplacement dans l'appartement, fatigue oculaire en fin de journée). L'utilisateur veut pouvoir continuer à interagir avec Bob sans rester collé à l'écran, et avoir une expérience plus "compagnon" — Bob qui parle, plutôt qu'une fenêtre qui s'écrit.

## Solution

Introduire un **mode Vocal** activable depuis le header du chat via une icône haut-parleur. Lorsqu'il est actif, chaque réponse de Bob est synthétisée vocalement en français et lue automatiquement en streaming pendant que le LLM continue à générer le reste de la réponse — l'audio démarre dès la première phrase complète, sans attendre la fin du message. La réponse texte continue de s'afficher normalement dans la conversation (le mode Vocal est additif, pas un remplacement).

La synthèse tourne entièrement en local via le modèle Kokoro (Apache 2.0, ~300 MB), exposé par le backend FastAPI existant. L'audio est streamé au front par chunks PCM 24 kHz sur la WebSocket déjà en place, et joué via Web Audio API avec une file de buffers ordonnancée pour une lecture continue sans gap.

Le toggle est session-only (reset au redémarrage). Si l'utilisateur envoie un nouveau message pendant que Bob parle, l'audio en cours est coupé et la synthèse pending annulée — Bob enchaîne sur la nouvelle question.

## User Stories

1. En tant qu'utilisateur de Bob, je veux activer le mode Vocal en un clic depuis le header du chat, afin de basculer rapidement entre lecture texte et écoute.
2. En tant qu'utilisateur, je veux voir clairement si le mode Vocal est ON ou OFF, afin de ne pas être surpris par une voix qui démarre.
3. En tant qu'utilisateur, je veux que Bob parle en français avec une voix naturelle, afin de comprendre les réponses sans effort.
4. En tant qu'utilisateur, je veux que la voix démarre dès la première phrase générée par le LLM, afin d'avoir un feedback rapide et ne pas attendre la fin complète du message.
5. En tant qu'utilisateur, je veux que la voix continue à jouer sans coupure ni gap entre les phrases successives, afin d'écouter Bob comme une voix continue et non comme une suite de fragments.
6. En tant qu'utilisateur, je veux continuer à voir le texte intégral de la réponse à l'écran pendant que Bob parle, afin de pouvoir relire ou reprendre en silencieux à tout moment.
7. En tant qu'utilisateur, je veux que Bob saute proprement les blocs de code lorsqu'il parle, afin de ne pas entendre du jargon imprononçable ou des backticks lus littéralement.
8. En tant qu'utilisateur, je veux que Bob ignore le markdown inline (asterisks, dièses, liens) quand il parle, afin d'avoir une élocution propre.
9. En tant qu'utilisateur, je veux pouvoir interrompre Bob simplement en envoyant un nouveau message, afin de ne pas attendre la fin d'une réponse devenue inutile.
10. En tant qu'utilisateur, je veux voir visuellement quelle bulle de message est en train d'être lue, afin de suivre où Bob en est dans la réponse.
11. En tant qu'utilisateur, je veux que le mode Vocal soit désactivable à chaud sans relancer Bob, afin de revenir au texte pur si je rentre dans un environnement bruyant ou silencieux.
12. En tant qu'utilisateur, je veux que le modèle vocal soit téléchargé automatiquement au premier démarrage, afin de ne pas avoir d'étape de setup manuelle.
13. En tant qu'utilisateur, je veux voir une indication claire pendant le téléchargement du modèle Kokoro, afin de comprendre pourquoi la première activation prend du temps.
14. En tant qu'utilisateur, je veux que la synthèse vocale échoue proprement avec un toast d'erreur, afin de continuer à utiliser Bob en texte si Kokoro est cassé.
15. En tant qu'utilisateur, je veux qu'une erreur TTS ne désactive pas le mode Vocal, afin que Bob retente automatiquement à la prochaine réponse.
16. En tant qu'utilisateur, je veux que la voix tourne 100 % en local, afin de garder mes conversations privées et de ne pas dépendre d'une API externe.
17. En tant qu'utilisateur, je veux que le mode Vocal soit session-only, afin que Bob redémarre toujours en mode texte (état par défaut conservateur).
18. En tant qu'utilisateur, je veux que la latence du premier son soit de l'ordre de 1–2 secondes après le début de la réponse LLM, afin d'avoir une expérience fluide.
19. En tant qu'utilisateur, je veux que la lecture de la réponse en cours ne bloque pas l'envoi d'un nouveau message, afin de pouvoir relancer à tout moment.
20. En tant qu'utilisateur, je veux que le mode Vocal coexiste avec les composants UI server-driven existants (cartes, listes), afin de ne pas perdre la richesse visuelle des réponses structurées.

## Implementation Decisions

### Moteur TTS
- TTS local via **Kokoro** intégré au backend par la lib `kokoro-onnx` (ONNX runtime Python).
- Voix par défaut : **`ff_siwis`** (seule voix FR officielle), speed = 1.0.
- Sample rate de sortie : **24 kHz, mono, PCM 16-bit ou float32** (à figer pendant l'implémentation selon ce que retourne `kokoro-onnx`).
- Modèle ONNX + fichiers de voix stockés dans `~/.bob/models/kokoro/`. **Auto-download au premier startup** si absents. Le backend ne sert pas de réponse TTS tant que le modèle n'est pas prêt — la première réponse en mode Vocal peut afficher un état "préparation de la voix".
- Inférence exécutée via `asyncio.to_thread` pour ne pas bloquer la boucle event loop FastAPI. Modèle chargé une seule fois au startup (ou paresseusement à la première requête, à arbitrer pendant l'implémentation).

### Pipeline texte → audio
- Pendant le stream LLM, les tokens sont accumulés dans un **segmenter** qui yield une phrase complète dès qu'il détecte une frontière (`.`, `!`, `?`, `\n\n`, ou heuristique combinée).
- Avant synthèse, la phrase est **nettoyée** :
  - Suppression des emphases markdown (`**`, `*`, `_`, `~~`, `#`, backticks inline).
  - Skip total des blocs de code (` ``` … ``` `) — pas de lecture, pas de substitution verbale.
  - Conversion des items de liste (`- `, `1. `) en prose simple.
  - Suppression des URLs (ou remplacement par "lien" — à figer pendant l'implémentation).
- Chaque phrase nettoyée est passée au TTS, le PCM résultant est envoyé au front en un ou plusieurs chunks WS.

### Protocole WebSocket
- Réutilise la WS existante (`ws_router`). Nouveaux types d'events sortants :
  - `audio_chunk` — payload : `{ type: "audio_chunk", msg_id: str, seq: int, pcm_b64: str, sample_rate: int }`.
  - `audio_end` — payload : `{ type: "audio_end", msg_id: str }`. Signale fin de la synthèse pour ce message.
  - `audio_error` — payload : `{ type: "audio_error", msg_id: str, reason: str }`. Optionnel, sinon erreur remontée via le canal d'erreur existant.
- Le client envoie un flag `voice: true` dans la requête chat lorsqu'il veut activer la synthèse pour cette réponse. Pas de toggle backend persistant : le backend ne synthétise que si le flag est présent dans la requête entrante.
- L'`msg_id` reprend l'identifiant déjà utilisé pour la réponse texte, permettant au front de coupler bulle ↔ audio.

### Annulation / interruption
- Si une nouvelle requête chat arrive pour la même session pendant qu'un `msg_id` antérieur stream encore de l'audio, le backend **annule la tâche TTS pending** (cancellation token / `asyncio.Task.cancel`) et n'envoie plus de chunks pour ce `msg_id`.
- Le front, à réception d'un nouveau stream LLM, **stoppe immédiatement** la lecture audio en cours et purge sa file de buffers.

### Modules backend (deep modules visés)
- **`tts_service`** — Interface : `synthesize(text: str, voice: str, speed: float) -> bytes`. Encapsule chargement modèle, threading, mapping voix. Stable, peu de changements attendus.
- **`text_segmenter`** — Interface : générateur async qui consomme deltas LLM et yield phrases nettoyées prêtes à parler. Pure logic, déterministe, sans I/O.
- **`model_downloader`** — Interface : `ensure_kokoro_ready() -> Path`. Vérifie présence locale, télécharge si nécessaire, retourne le chemin. Hide reachable source (HF Hub URL, hash check).
- **`ws_router`** (modifié) — Reconnaît flag `voice` dans la requête chat ; quand actif, branche le pipeline segmenter → tts_service → events audio. Garde une map `{msg_id: task}` pour annulation.
- **`chat_service`** (modifié) — Si `voice` actif, alimente le segmenter au fil du stream LLM.
- **`config`** (modifié) — Paramètres : chemin modèle, voix par défaut, sample rate attendu, URL de download Kokoro.

### Modules frontend
- **`audioPlayer`** (deep module, probablement sous `src/audio/`) — Interface : `enqueue(pcmB64, sampleRate, msgId)`, `stop(msgId?)`, `onSpeakingChange(callback)`. Gère AudioContext, scheduling continu via `start(when)`, file FIFO de buffers, transitions speaking → idle. Hide complétement Web Audio.
- **`useVoiceMode`** (hook) — Expose `{ voiceEnabled, toggle }`. État interne dans React state ou Zustand (session-only, pas de persistance localStorage pour le MVP).
- **`useWebSocket`** (modifié) — Dispatch des events `audio_chunk` / `audio_end` / `audio_error` vers `audioPlayer`.
- **`chatStore`** (modifié) — Track `speakingMsgId: string | null` pour piloter l'indicateur visuel.
- **`ChatView`** (modifié) — Bouton toggle voix dans le header (icône haut-parleur), lit `voiceEnabled` du hook, joint le flag `voice` à chaque envoi de message.
- **`ChatMessageBlock`** (modifié) — Affiche une icône onde sonore animée tant que `speakingMsgId === message.id`.
- **`types/ws`** (modifié) — Ajoute les types d'events audio au discriminated union existant.

### Erreurs et fallback
- Échec download modèle, échec inférence Kokoro, voix introuvable → backend émet `audio_error` (ou erreur générique) → front affiche un toast "TTS indisponible : <raison>" via le composant `Toast` existant.
- La réponse texte est livrée normalement même en cas d'échec TTS.
- Le toggle voix reste ON après une erreur (retente au prochain message).

### États visuels
- Icône toggle header : deux états binaires (haut-parleur barré / haut-parleur). Couleur active reprend le style du picker provider LLM voisin.
- Icône lecture sur bulle : petite animation onde sonore, visible uniquement tant que `speakingMsgId === msg.id`.
- Téléchargement initial du modèle : toast info ou état dédié dans le header pendant la durée du download (à figer pendant l'implémentation — un toast progress suffit pour le MVP).

### Dépendances ajoutées
- Python : `kokoro-onnx` (et son backend ONNX runtime). Mise à jour `pyproject.toml` + `uv.lock`.
- Front : aucune dépendance externe nouvelle (Web Audio est natif).

## Testing Decisions

Pas de tests automatisés pour ce MVP. Validation manuelle :
- Activer le mode Vocal, envoyer un message court → Bob lit la réponse en français, audio fluide.
- Envoyer un message qui produit une réponse longue avec un bloc de code → la voix saute le bloc proprement et reprend après.
- Envoyer un nouveau message pendant que Bob parle → audio coupé, nouvelle réponse synthétisée.
- Désactiver le mode Vocal en plein milieu d'une lecture → audio coupé, le toggle reste OFF.
- Supprimer manuellement le dossier `~/.bob/models/kokoro/` → au prochain message en mode Vocal, download relancé, toast info, puis lecture.
- Couper le réseau au moment du download initial → toast erreur, fallback texte, mode Vocal toujours activable pour retry.

## Out of Scope

- **Reconnaissance vocale (STT)** : pas de micro, pas de push-to-talk, pas de wake word. Mode Vocal MVP = sortie uniquement.
- **Multi-voix** : pas de picker UI pour changer de voix. `ff_siwis` figé.
- **Réglage vitesse** : pas de slider. Speed = 1.0 figé.
- **Persistance cross-session** du toggle voix : reset au redémarrage de Bob.
- **Réglages avancés** : pas de pitch, pas d'égalisation, pas de choix de format audio (PCM brut figé).
- **TTS API externe** (OpenAI, ElevenLabs) : non considéré pour ce MVP, local only.
- **Replay manuel** d'un ancien message via bouton dédié sur la bulle.
- **Sous-titres karaoké / surlignage du mot en cours** : pas de timing fin token ↔ audio.
- **GPU CoreML provider** pour Kokoro : démarrage en CPU ONNX runtime, accélération à itérer ultérieurement si latence insuffisante.
- **Compression audio** (Opus, MP3) sur le fil WS : PCM brut base64 suffit en local.
- **Détection automatique de langue** de la réponse pour switcher de voix : voix FR figée même si Bob répond en anglais (cas marginal pour le MVP).
- **Multi-device / sync préférences** : Bob est mono-poste local.

## Further Notes

- Le mode Vocal s'ajoute au pipeline server-driven UI existant (`ui_registry`, `Dispatcher`, composants `ChatMessageBlock`) sans le remplacer. Les composants visuels riches (cartes, listes) continuent de s'afficher ; seule la portion texte du message est synthétisée.
- Latence cible end-to-end (clic envoyer → premier son) : **< 3 s** sur Mac Apple Silicon, dont ~1–2 s pour la synthèse Kokoro de la première phrase.
- Kokoro FR (`ff_siwis`) a une qualité correcte mais limitée par rapport aux voix EN officielles. Si la qualité FR devient un point bloquant, deux options à évaluer plus tard : (a) attendre les voix FR mises à jour upstream, (b) basculer sur un autre moteur (Piper FR, OpenAI TTS) — déjà discuté en grill, hors scope MVP.
- Le découpage en deep modules (`tts_service`, `text_segmenter`, `model_downloader`, `audioPlayer`) est volontairement conçu pour permettre un remplacement isolé du moteur TTS plus tard sans toucher au pipeline ni au front.
- `~/.bob/models/` est un nouvel emplacement standardisé pour les artefacts locaux de Bob ; à documenter dans `CLAUDE.md` et `README.md` après merge.
- Le hook `useVoiceMode` est volontairement session-only pour le MVP. Si l'usage montre que les utilisateurs activent systématiquement le mode au démarrage, basculer en `localStorage` est trivial et sans impact backend.
- Aucune ADR à respecter spécifiquement sur ce périmètre (vérifié — pas de dossier `adr/` actuellement dans le repo).
