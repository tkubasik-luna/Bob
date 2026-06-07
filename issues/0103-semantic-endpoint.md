## Parent

`prd/0016-jarvis-realtime-fullduplex.md` (Endpointing ; Annexes B, H).

## What to build

L'**endpoint sémantique** : décider que le tour user est fini par le sens, pas seulement
par le silence — pour ne pas couper l'utilisateur qui hésite, et démarrer la réponse plus
tôt sur une clause complète.

- L'`Endpointer` (de S4) consomme désormais **deux sources** : le filet **VAD silence**
  (existant) ET le signal **`user_turn_complete`** émis par le Thinker dans son snapshot
  (S6).
- Anti-faux-positif (Annexe H) : `user_turn_complete` ne déclenche l'`endpoint` que
  **confirmé par le partiel suivant stable** ; sinon le VAD silence reste le filet.
- Si la phrase semble inachevée malgré une pause VAD courte, l'endpoint est **retenu**
  (le Thinker n'a pas encore signalé la complétude).

## Acceptance criteria

- [ ] `Endpointer` fusionne VAD silence + `user_turn_complete` du Thinker.
- [ ] `user_turn_complete` déclenche l'endpoint **uniquement** s'il est confirmé par un partiel suivant stable (anti faux-positif).
- [ ] Une clause complète déclenche l'endpoint **plus tôt** que le seuil de silence seul (mesurable).
- [ ] Une pause en milieu de phrase incomplète **ne** déclenche **pas** l'endpoint (Bob attend).
- [ ] Le VAD silence reste le filet si le signal sémantique n'arrive jamais.
- [ ] Scénario `bob attest` (deux cas) : (a) clause complète → endpoint anticipé (assert `t_endpoint` < seuil silence) ; (b) hésitation mi-phrase → pas d'endpoint prématuré (Bob ne répond pas avant la complétude).
- [ ] Tests unit : `Endpointer` (séquences combinées VAD + `user_turn_complete` + confirmation, cas complet/incomplet/timeout filet).

## Blocked by

- `issues/0102-thinker-livestate-provider.md`
