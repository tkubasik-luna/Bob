## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Backchannels ; Annexes B, A.2).

## What to build

Les **backchannels** : Bob place de brefs accusés de réception ("mm", "ok je vois")
**dans les pauses** de l'utilisateur, pour un échange vivant.

- Déclenchés par le **Thinker** + un **seuil de proactivité** (logique inner-thoughts
  "when-to-speak" : pertinence + décroissance-silence), pendant `user_speaking` sur un
  `vad_pause` (Annexe B : backchannel = **action**, pas un état → pas d'overlap).
- **Jamais par-dessus la parole** (on attend une pause). Synthétisés en court via Kokoro.
- Émet `backchannel {turn_id, token, ts}` (Annexe A.2) ; mark/cible latence < 500 ms.

## Acceptance criteria

- [ ] Backchannel émis uniquement sur `vad_pause` pendant `user_speaking` (jamais pendant la parole active).
- [ ] Déclenchement gated par le Thinker + seuil de proactivité (pas systématique).
- [ ] Token court synthétisé via Kokoro et joué ; n'interrompt pas le tour (pas de transition de floor).
- [ ] Event `backchannel` émis ; `backchannel_ms` mesuré, cible < 500 ms.
- [ ] Scénario `bob attest` : un tour avec une pause → assert `backchannel` émis pendant la pause ; un tour sans pause / parole continue → assert **aucun** backchannel pendant la parole.
- [ ] Tests unit : logique de déclenchement (gate proactivité + pause requise + décroissance-silence).

## Blocked by

- `issues/0102-thinker-livestate-provider.md`
