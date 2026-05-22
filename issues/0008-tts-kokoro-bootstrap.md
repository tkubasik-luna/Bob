## Parent

`prd/0002-voice-mode.md`

## What to build

Mettre en place le moteur TTS local Kokoro côté backend FastAPI, isolé du reste du pipeline. Ajouter la dépendance `kokoro-onnx`, créer le deep module `tts_service` qui charge le modèle une seule fois et expose `synthesize(text, voice, speed) -> PCM bytes` exécuté hors event loop via `asyncio.to_thread`. Créer le deep module `model_downloader` qui garantit la présence du modèle ONNX et des voix dans `~/.bob/models/kokoro/`, en téléchargeant depuis Hugging Face au premier lancement si absent.

Exposer la capacité via un endpoint HTTP de test (par exemple `POST /debug/tts`) qui prend un payload `{ text: string }`, synthétise avec la voix `ff_siwis` à speed 1.0, et retourne le PCM 24 kHz brut (ou un WAV équivalent pour faciliter le test manuel). Pas d'intégration WS, pas de front, pas de segmenter — slice purement backend, vérifiable via `curl` ou un client HTTP.

## Acceptance criteria

- [ ] Dépendance `kokoro-onnx` ajoutée dans `pyproject.toml` et `uv.lock` régénéré
- [ ] `model_downloader.ensure_kokoro_ready()` vérifie `~/.bob/models/kokoro/`, télécharge les artefacts manquants, log la progression et retourne le chemin local
- [ ] `tts_service` charge le modèle Kokoro une fois (au startup ou paresseusement à la première requête) et expose `synthesize` qui tourne via `asyncio.to_thread`
- [ ] Endpoint `POST /debug/tts` accepte `{ text: string }` et retourne un PCM 24 kHz exploitable (lisible par `ffplay`/`afplay` après wrap WAV minimal)
- [ ] Un test manuel : `curl -X POST … -d '{"text":"Bonjour, je suis Bob."}'` produit un fichier audio audible en français avec la voix `ff_siwis`
- [ ] Suppression du dossier `~/.bob/models/kokoro/` relance le download automatiquement au prochain appel
- [ ] L'event loop FastAPI reste responsive pendant une synthèse (autre requête HTTP non bloquée)

## Blocked by

None - can start immediately
