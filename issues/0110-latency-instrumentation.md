## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Latence — DoD produit ; Annexe F).

## What to build

L'**instrumentation de latence** + les assertions de DoD qui rendent « temps réel »
mesurable et opposable.

- Émettre l'event **`turn_latency {turn_id, marks, derived}`** (Annexe A.2/F) en fin de
  tour, agrégeant les **marks** posés par les slices amont (`t_first_mic_frame`,
  `t_first_partial`, `t_endpoint`, `t_draft_ready`, `t_commit_decision`,
  `t_first_audio_chunk`, `t_tts_end`, et sur barge-in `t_bargein_detected`/`t_cut`) sur une
  horloge **monotone serveur**.
- Calculer les **dérivés** : `endpoint_to_first_audio_ms`, `bargein_cut_ms`,
  `backchannel_ms`, `draft_hit`.
- Étendre le harnais avec les assertions **`latency_lt_ms` / `bargein_within_ms`** (et
  l'option `--deep` `transcript_roundtrip_similarity_gte` : TTS→whisper→compare).
- Persister `turn_latency` dans `voice_turns.latency_json` (S13).

Cross-cutting : peut démarrer dès S4 (marks de base) ; la **validation des cibles** est
faite une fois le pipeline complet (S8) en place.

## Acceptance criteria

- [ ] Event `turn_latency` émis en fin de tour avec `marks` (monotone serveur) + `derived` (Annexe F).
- [ ] Dérivés calculés : `endpoint_to_first_audio_ms`, `bargein_cut_ms`, `backchannel_ms`, `draft_hit`.
- [ ] Assertions harnais `latency_lt_ms` (mark→mark) et `bargein_within_ms` implémentées ; option `--deep` `transcript_roundtrip_similarity_gte`.
- [ ] `turn_latency` persisté dans `voice_turns.latency_json`.
- [ ] Cibles de DoD vérifiées par scénarios (une fois S8 en place) : barge-in **< 300 ms**, endpoint→premier audio **< 800 ms** (committé) / **< 1.5 s** (froid), backchannel **< 500 ms**.
- [ ] Scénario `bob attest` : un tour committé → assert `latency_lt_ms endpoint→first_audio max:800` ; un barge-in → `bargein_within_ms max:300`.
- [ ] Tests unit : calcul des dérivés depuis les marks ; assertions latence (`latency_lt_ms`, `bargein_within_ms`) sur events synthétiques.

## Blocked by

- `issues/0100-fullduplex-loop-bare.md`
