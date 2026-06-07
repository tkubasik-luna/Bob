## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Orchestration temps réel ; Annexes B, A.2, F).

## What to build

La **boucle full-duplex nue** : de la parole à la réponse vocale, **SANS Thinker ni
Draft**. Prouve l'ossature audio-in → cerveau existant → audio-out.

- **VAD** sur les frames → events `vad_speech_start` / `vad_pause`.
- **`Endpointer`** (filet silence seul ici, ~500–700 ms) → `endpoint`.
- **`TurnFsm`** : implémente la table Annexe B (transitions de base, **barge-in en S5**) :
  `idle → user_speaking → thinking → bob_speaking → idle`.
- À l'`endpoint` : freeze du transcript final → **say-path Jarvis EXISTANT** sur ce
  transcript → TTS sortant (réutilise Kokoro + `speech_delta`/`audio_chunk`).
- Émet `turn_state {turn_id,from,to,reason,ts}` (Annexe A.2) à chaque transition.
- Marks de latence de base (Annexe F) : `t_first_mic_frame`, `t_endpoint`,
  `t_first_audio_chunk` (enrichis par S14).
- **Path texte hybride conservé** : un `client_text` déclenche un tour via la même
  convergence en aval (zéro régression sur l'entrée texte existante).

## Acceptance criteria

- [ ] VAD sur frames → `vad_speech_start` / `vad_pause`.
- [ ] `Endpointer` silence floor (~500–700 ms réglable) → `endpoint`.
- [ ] `TurnFsm` implémente les transitions de base de la table Annexe B (sans barge-in).
- [ ] À l'`endpoint` : transcript figé → say-path Jarvis existant → TTS out.
- [ ] `turn_state` émis à chaque transition avec `from`/`to`/`reason`.
- [ ] Invariant assertable : jamais deux `turn_id` en `bob_speaking` simultanément.
- [ ] `client_text` déclenche un tour via la même convergence (hybride) ; le path texte existant n'a aucune régression.
- [ ] Marks de latence de base émis dans `turn_latency.marks`.
- [ ] Scénario `bob attest` (`--text` ET `--audio`) : assert la séquence `turn_state` `idle→user_speaking→thinking→bob_speaking→idle` + `audio_chunks_gte 1`.
- [ ] Tests unit : `TurnFsm` (transitions de base exhaustives) ; `Endpointer` (séquences silence) ; VAD (seuils).

## Blocked by

- `issues/0099-listen-stt-pipeline.md`
