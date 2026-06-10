# 0119 — Barge-in zero-grace : couper Bob en <300 ms

## Parent

`prd/0018-oral-latency-reliability.md` (Module 2, barge-in)

## What to build

Donner au barge-in une politique d'annulation **séparée et sans grâce** : dès la confirmation d'interruption, hard cancel immédiat du say-path (via le cancel unitaire du pipeline TTS si 0121 est mergé, sinon le mécanisme actuel) et des boucles Thinker/Draft — aucune fenêtre de grâce coopérative sur ce chemin. L'endpoint garde sa grâce plafonnée (0118) ; le barge-in n'en a aucune. Cible Annexe F du PRD 0016 : interruption effective (plus d'audio émis) en moins de 300 ms après confirmation, mesurable via les marks 0117.

## Acceptance criteria

- [ ] Après confirmation de barge-in, plus aucun chunk audio n'est envoyé au client dans les 300 ms (fake clock + fake TTS, timestamps des événements).
- [ ] Les boucles Thinker/Draft sont hard-cancelled sans attendre de grâce, même si leur cancel coopératif stalleraient.
- [ ] La FSM transite correctement vers l'écoute du nouveau tour utilisateur ; le texte déjà prononcé est tronqué proprement dans le transcript.
- [ ] La politique endpoint (grace cap 250 ms) reste inchangée — les deux chemins sont distincts et testés séparément.
- [ ] Le temps de coupure apparaît dans le résumé 0117 du turn interrompu.
- [ ] Tests : barge-in pendant streaming TTS, pendant THINKING, et avec passes d'anticipation en vol.

## Blocked by

- `issues/0118-endpoint-concurrent-commit.md`
