# 0118 — Endpoint concurrent : freeze parallèle + grace cap + finalize non bloquant

## Parent

`prd/0018-oral-latency-reliability.md` (Module 2, cœur)

## What to build

Paralléliser le chemin endpoint du full-duplex loop. À la détection de fin de parole :

- le gel du Thinker et du Draft s'exécute **concurremment** (fan-out) au lieu de séquentiellement ;
- la grâce de cancel coopératif est plafonnée à 250 ms (setting, contre 2 s aujourd'hui) ; au-delà, hard cancel ;
- la finalisation STT (passe whisper full-buffer) tourne **en parallèle** du gel — le say-path est spawné dès que la décision du commit gate est disponible, sans attendre la fin de tous les nettoyages.

Les marks `loops_frozen`, `stt_finalized`, `gate_decided` (0117) encadrent le chemin pour mesurer le gain. Cible : jusqu'à −2 s sur endpoint → premier audio quand une passe d'anticipation est en vol.

## Acceptance criteria

- [ ] Avec un fake Thinker ET un fake Draft qui stalleraient 2 s en cancel, le say-path démarre dans la fenêtre du grace cap (250 ms + epsilon), vérifié sous fake clock.
- [ ] Gel Thinker et gel Draft démarrent au même instant (timestamps concurrents, pas séquentiels).
- [ ] La finalisation STT ne retarde plus le lancement du say-path ; le transcript final reste correct (le say-path consomme le transcript finalisé quand il en a besoin).
- [ ] Le grace cap est un setting, défaut 250 ms.
- [ ] Le résumé 0117 d'un turn montre les nouvelles durées d'étapes (loops_frozen, stt_finalized, gate_decided).
- [ ] Aucune régression : un endpoint sans passe en vol se comporte comme avant ; le draft commité est toujours adopté correctement.
- [ ] Tests sur l'ordre/timing des événements externes du loop, fixtures fake clock + fake STT/Thinker/Draft existantes.

## Blocked by

- `issues/0117-turn-latency-metrics.md`
