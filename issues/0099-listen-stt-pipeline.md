## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Pipeline audio & STT ; Annexes A.1, A.2, G).

## What to build

Le tracer **« Listen »** bout-en-bout : de la capture micro à l'événement transcript.

- **Frontend `MicCapture`** : `getUserMedia` (selon le chemin retenu par S1) + AudioWorklet,
  downsample **16 kHz mono**, envoi de **frames binaires WS taggées `0x01`** (Annexe A.1)
  quand le mode voix est ON (`voice_start` / `voice_stop`). Mic possédé par la fenêtre HUD
  `new`.
- **Backend canal binaire** : nouveau mode binaire dans `ws_router` (le WS est JSON-only
  aujourd'hui) ; décodage des frames PCM 16 kHz s16le.
- **`SttEngine`** : wrapper **whisper.cpp (Metal/CoreML)**, modèle par défaut
  **large-v3-turbo** (download lazy à la Kokoro), interface stable frames→partiels+final,
  moteur swappable.
- **Events** : `stt_partial {turn_id,text,stable_prefix_len,ts}` et
  `stt_final {turn_id,text,ts}` (Annexe A.2, `category:"voice"`), via `emit_event` avec
  **`debug_payload` scrubbé** (contenu utilisateur tronqué vers le ring buffer).
- **Harnais `--audio`** : `inject_audio fixture.wav` → frames binaires → assertion
  `stt_final_matches`.

## Acceptance criteria

- [ ] `MicCapture` : `getUserMedia` (chemin S1) + AudioWorklet downsample 16 kHz mono + envoi frames binaires WS (tag `0x01`) entre `voice_start` et `voice_stop`.
- [ ] Canal WS binaire serveur : décodage des frames PCM 16 kHz s16le, routage vers `SttEngine`.
- [ ] `SttEngine` whisper.cpp (Metal), modèle large-v3-turbo, **download lazy** (toast type `tts_preparing`).
- [ ] Events `stt_partial` et `stt_final` émis en FR avec `turn_id` ; `stable_prefix_len` renseigné sur les partiels.
- [ ] `debug_payload` scrubbé vers le ring buffer ; `payload` complet vers le client.
- [ ] Dégradation (Annexe G) : modèle whisper absent → download + toast ; STT échoue en cours → tour avorté proprement (`end_reason:error`), retour `idle`, pas de crash.
- [ ] Harnais étendu : `inject_audio fixture.wav` + assertion `stt_final_matches` (contains/regex).
- [ ] Scénario `bob attest --audio` : fixture FR connue → `stt_final` contient le texte attendu.
- [ ] Tests unit : `SttEngine` (fixture WAV → transcript attendu, moteur réel marqué lent) ; décodeur de frames binaires ; `MicCapture` downsample (frontend).

## Blocked by

- `issues/0097-spike-aec-getusermedia.md`
- `issues/0098-attest-harness-skeleton.md`
