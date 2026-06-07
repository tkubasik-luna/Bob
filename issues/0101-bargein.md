## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Barge-in ; Annexes B, A.2, F, G).

## What to build

Le **barge-in** : l'utilisateur coupe Bob en pleine phrase.

- **`BargeInController`** : pendant `bob_speaking`, VAD détecte la parole user ; on attend
  une **fenêtre de confirmation ~200–300 ms** de parole continue (filtre les
  backchannels/bruits courts) avant de déclencher.
- **Action** (transition `bob_speaking → user_speaking`, Annexe B) : annuler le stream LLM
  en cours + le TTS, **committer dans l'historique le texte déjà prononcé**
  (`committed_spoken_text` — dérivé des chunks TTS effectivement joués), relancer le Thinker.
- Émet `bargein {turn_id, detected_ts, cut_ts, committed_spoken_text}` (Annexe A.2) + les
  marks `t_bargein_detected` / `t_cut` (Annexe F).
- **Dégradation** (Annexe G) : si l'AEC est KO (chemin S1 = échec runtime), dégrader en
  **half-duplex gate** (mute mic pendant `bob_speaking`) comme filet, avec flag visible.

## Acceptance criteria

- [ ] `BargeInController` déclenche le barge-in après ~200–300 ms (réglable) de parole continue détectée pendant `bob_speaking` ; pas de coupure en-deçà de la fenêtre.
- [ ] Au déclenchement : stream LLM annulé + TTS annulé + `committed_spoken_text` calculé (ce qui a réellement été joué) et persisté dans l'historique.
- [ ] FSM transite `bob_speaking → user_speaking` ; le Thinker repart.
- [ ] Event `bargein` émis avec `detected_ts` / `cut_ts` / `committed_spoken_text` ; marks latence émis.
- [ ] `bargein_cut_ms = t_cut − t_bargein_detected` mesuré ; cible **< 300 ms**.
- [ ] Half-duplex gate de secours câblé si l'AEC est indisponible (filet, flag visible).
- [ ] Scénario `bob attest` : injecter de la parole à `+200 ms` pendant `bob_speaking` → assert `bargein_within_ms max:300` + `committed_equals_spoken` + FSM atteint `user_speaking`.
- [ ] Tests unit : `BargeInController` (timeline simulée : bruit court n'interrompt pas, parole > fenêtre interrompt) ; calcul de `committed_spoken_text` depuis les chunks joués.

## Blocked by

- `issues/0100-fullduplex-loop-bare.md`
