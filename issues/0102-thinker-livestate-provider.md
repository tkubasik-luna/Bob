## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Penser en parallèle ; Annexes A.2, H).

## What to build

Le **Thinker** : compréhension de fond en parallèle de la parole.

- **`ThinkerLoop`** : sous-agent de fond (sous le `TaskGroup` existant), modèle mini (1–3B
  par défaut, rôle `thinker` lu depuis la sélection — voir S10). Tourne sur le transcript
  partiel, **debounced ~250 ms** (Annexe H), jamais plus d'une inférence en vol par tour.
- **`LiveTranscriptState`** : store en mémoire que le Thinker met à jour ; chaque snapshot
  porte un `seq` croissant (les `seq` périmés sont ignorés — anti-stale).
- **`ThinkerStateProvider`** : **provider pur** (no I/O/time/random dans `entries()`) qui lit
  le dernier snapshot et émet un `ContextEntry` (même pattern que `StateBlockProvider` →
  `TaskStore`). Branché dans la policy d'assemblage ; l'assemblage reste pur, déclenché par
  l'endpoint du FSM.
- **`Speaker`** : le say-path Jarvis consulte le dernier snapshot (Restate-Consult-Solve).
- Émet `thinker_snapshot {turn_id, seq, corrected_text, variables, next_step_plan,
  user_turn_complete, ts}` (Annexe A.2 ; `user_turn_complete` **câblé en S7**).
- **Annulation coopérative** sur `endpoint` / `bargein` / `voice_stop` (cancel + grâce +
  hard-kill, comme les sous-agents).

## Acceptance criteria

- [ ] `ThinkerLoop` tourne en fond sur les `stt_partial`, debounced ~250 ms, ≤ 1 inférence en vol par tour.
- [ ] `LiveTranscriptState` : snapshots avec `seq` croissant ; un `seq` < dernier vu est ignoré.
- [ ] `ThinkerStateProvider` pur (testable golden-prompt) émet un `ContextEntry` depuis le snapshot ; intégré à la policy d'assemblage.
- [ ] Le `Speaker` (say-path) consulte le dernier snapshot au moment de l'assemblage (déclenché par l'endpoint).
- [ ] Events `thinker_snapshot` émis (avec `debug_payload` scrubbé) ; `user_turn_complete` présent dans le payload (valeur exploitée en S7).
- [ ] Annulation coopérative sur `endpoint`/`bargein`/`voice_stop`.
- [ ] Le rôle `thinker` lit sa sélection LLM (S10) ; défaut mini local.
- [ ] Scénario `bob attest` : un tour → assert ≥ 1 `thinker_snapshot` émis + le contexte d'assemblage du Speaker contient le snapshot (event/marqueur dédié).
- [ ] Tests unit : `LiveTranscriptState` (anti-stale `seq`) ; `ThinkerStateProvider` (projection snapshot → `ContextEntry`, golden-prompt) ; debounce/cancellation du loop.

## Blocked by

- `issues/0100-fullduplex-loop-bare.md`
