## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Rétention & privacy ; Annexes E.1, E.2, E.3).

## What to build

La **persistance + rétention bornée** des échanges vocaux (debug/replay/tuning), sans
saturer le disque.

- **Migrations SQL** `0010_voice_turns.sql` et `0011_voice_audio_blobs.sql` (Annexe E) :
  `voice_turns` (turn_id, jarvis_msg_id, final_transcript, spoken_text, started/ended_at,
  end_reason, draft_outcome, latency_json) ; `voice_audio_blobs` (turn_id, kind
  `mic_in`/`tts_out`, path WAV sur disque, bytes, created_at).
- Wiring de persistance : à la fin d'un tour, écrire la ligne `voice_turns` + les blobs
  audio (fichiers WAV sur disque, chemin en DB) ; le **transcript final entre dans
  l'historique Jarvis** (lien `jarvis_msg_id`).
- **`VoiceRetentionPolicy`** : purge auto, **caps séparés** — `voice_audio_blobs` borné par
  **taille** (défaut 1.5 Gio, plus vieux d'abord, supprime fichier + ligne) ; `voice_turns`
  borné par **âge** (défaut 30 j). Réglable en settings. Esprit `EventRetentionPolicy`.

## Acceptance criteria

- [ ] Migrations `0010`/`0011` créent `voice_turns` et `voice_audio_blobs` (runner idempotent existant).
- [ ] Fin de tour : ligne `voice_turns` écrite (transcript final, `spoken_text`, `end_reason`, `draft_outcome`, `latency_json`) + blobs audio (`mic_in`/`tts_out`) en fichiers WAV, chemins en DB.
- [ ] Transcript final lié à l'historique Jarvis (`jarvis_msg_id`).
- [ ] `VoiceRetentionPolicy` : purge audio par taille (défaut 1.5 Gio, plus vieux d'abord, fichier + ligne supprimés) ; purge `voice_turns` par âge (défaut 30 j) ; bornes réglables.
- [ ] Le toggle voix OFF coupe réellement la capture (pas d'écriture quand off).
- [ ] Scénario `bob attest` : un tour vocal → assert ligne `voice_turns` + blob créés ; un run avec rétention forcée petite → assert purge des plus vieux.
- [ ] Tests unit : `VoiceRetentionPolicy` (éviction par taille, par âge, caps séparés audio/texte) ; persistance (round-trip `voice_turns`/`voice_audio_blobs`).

## Blocked by

- `issues/0100-fullduplex-loop-bare.md`
