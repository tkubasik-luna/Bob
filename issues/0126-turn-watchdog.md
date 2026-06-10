# 0126 — TurnWatchdog : aucun turn perdu sans signal

## Parent

`prd/0018-oral-latency-reliability.md` (Module 6)

## What to build

Borner chaque turn utilisateur (texte ou voix) par un budget wall-clock supervisé :

- expiration → événement `turn_timeout`, FSM remise en état sain, et fallback court (verbal sur le chemin voix, texte sinon) à la place du silence éternel ;
- un timeout TTFT court (~15–30 s, setting) distinct du budget de complétion détecte « le provider n'a jamais commencé à répondre » ;
- les awaits réseau du chemin de turn sans garde aujourd'hui (régénération de summary, synthèse proactive, preload TTS, streaming TTS) reçoivent des timeouts explicites avec sémantique degrade-and-continue (on continue avec un summary incomplet, on saute l'annonce proactive, etc.).

S'appuie sur le TaskSupervisor (0124) pour la supervision du watchdog lui-même.

## Acceptance criteria

- [ ] Un provider LLM fake qui ne répond jamais → `turn_timeout` émis dans le budget, FSM saine, fallback délivré au client (fake clock).
- [ ] Un provider qui commence à streamer puis stalle → coupé au budget de complétion, pas au TTFT.
- [ ] Une régénération de summary qui hang n'empêche pas le turn de continuer (degrade-and-continue loggé).
- [ ] Un preload/streaming TTS qui hang produit un signal client au lieu d'un « tts_ready » sans audio.
- [ ] Budgets TTFT et complétion en settings ; valeurs distinctes pour le chemin voix.
- [ ] Aucun timeout déclenché sur un turn nominal rapide.
- [ ] Tests : fakes qui stallent à chaque étape gardée → événement + fallback observés.

## Blocked by

- `issues/0124-task-supervisor.md`
