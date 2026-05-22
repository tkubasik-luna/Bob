## Parent

`prd/0002-voice-mode.md`

## What to build

Permettre à l'utilisateur d'interrompre Bob simplement en envoyant un nouveau message pendant qu'il parle. La slice couvre les deux côtés du contrat : annulation côté backend et stop côté front.

Backend : maintenir une map `{ msg_id: tts_task }` dans `ws_router`. À l'arrivée d'une nouvelle requête chat pour la session, parcourir les tâches TTS encore actives et appeler `cancel()` sur chacune. Plus aucun `audio_chunk` n'est émis pour les `msg_id` annulés. Émettre éventuellement un `audio_end` final (ou un nouvel event `audio_cancelled`, choix à figer pendant l'implémentation) pour permettre au front de propre la file proprement.

Front : à réception d'une nouvelle réponse LLM (premier delta texte pour un nouveau `msg_id`), appeler `audioPlayer.stop()` immédiatement. La file de buffers est purgée, l'AudioContext continue de tourner pour la suite. Ignorer tout `audio_chunk` tardif arrivant avec un `msg_id` antérieur.

## Acceptance criteria

- [ ] Envoyer un message → Bob commence à parler → envoyer un second message avant la fin → l'audio s'arrête net en moins de ~200 ms
- [ ] La nouvelle réponse est lue à voix haute normalement, sans résidu de l'ancienne
- [ ] Backend : aucune tâche TTS de l'ancien `msg_id` ne continue à tourner en arrière-plan (pas de waste CPU/GPU)
- [ ] Front : les `audio_chunk` tardifs de l'ancien `msg_id` (arrivés après annulation) sont ignorés, pas joués
- [ ] Mode Vocal OFF : envoyer un message pendant qu'une ancienne réponse texte se termine ne déclenche aucun audio (cohérence)
- [ ] Aucune exception ni log d'erreur silencieuse côté backend lors de l'annulation

## Blocked by

- `issues/0010-voice-mode-e2e-full-message.md`
