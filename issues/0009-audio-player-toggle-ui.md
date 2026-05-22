## Parent

`prd/0002-voice-mode.md`

## What to build

Mettre en place côté front l'infrastructure de lecture audio et le toggle UI du mode Vocal, sans branchement WS. Créer le deep module `audioPlayer` qui encapsule complètement Web Audio API : exposer `enqueue(pcmB64, sampleRate, msgId)`, `stop(msgId?)`, et un mécanisme d'observation de l'état "speaking" (callback ou événement). Gérer l'AudioContext, la file FIFO de buffers, et l'ordonnancement via `start(when)` pour garantir une lecture continue sans gap entre chunks successifs.

Créer le hook `useVoiceMode` qui expose `{ voiceEnabled, toggle }` en state React session-only (pas de localStorage). Ajouter un bouton toggle dans le header de `ChatView` (icône haut-parleur barré vs haut-parleur, style cohérent avec le picker provider LLM voisin).

Pour démonstration, brancher un bouton dev/temporaire qui envoie un PCM mock (synthétisé front, par exemple un sinus 440 Hz de 1 s) dans `audioPlayer.enqueue` afin de valider la chaîne de lecture isolément.

## Acceptance criteria

- [ ] Module `audioPlayer` créé avec API `enqueue(pcmB64, sampleRate, msgId)`, `stop(msgId?)`, observation état speaking
- [ ] AudioContext initialisé paresseusement (au premier `enqueue` ou geste utilisateur) pour respecter les contraintes d'autoplay des webviews
- [ ] Plusieurs chunks `enqueue` consécutifs sont lus sans coupure ni gap audible
- [ ] `stop()` interrompt immédiatement la lecture et purge la file
- [ ] Hook `useVoiceMode` expose toggle session-only, état perdu au reload (vérifié)
- [ ] Bouton haut-parleur visible dans le header `ChatView`, ON/OFF distinct visuellement
- [ ] Bouton dev temporaire (peut être conditionné à un flag) envoie un PCM sinus 440 Hz à `audioPlayer` et joue le son en cliquant
- [ ] Aucun warning Web Audio en console pendant un cycle enqueue → stop

## Blocked by

None - can start immediately
