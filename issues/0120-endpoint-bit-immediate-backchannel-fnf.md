# 0120 — Signal turn-complete immédiat + backchannel fire-and-forget

## Parent

`prd/0018-oral-latency-reliability.md` (Module 2, partiel)

## What to build

Deux micro-latences du chemin voix :

1. Le bit sémantique `user_turn_complete` produit par le Thinker contourne le debounce de cadence (250 ms) : dès qu'une passe Thinker conclut que le tour utilisateur est fini, le signal se propage immédiatement vers la détection d'endpoint, même si la prochaine passe d'inférence reste debouncée.
2. La synthèse de backchannel (« mm », « ok ») devient fire-and-forget : spawnée en tâche de fond supervisée (erreurs étouffées et loggées), plus jamais awaité dans la boucle de frames.

## Acceptance criteria

- [ ] Avec un fake Thinker qui pose `user_turn_complete`, le signal atteint la logique d'endpoint sans attendre la fenêtre de debounce (vérifié par timestamps sous fake clock).
- [ ] Le debounce des passes d'inférence Thinker reste inchangé pour le reste du payload.
- [ ] Un backchannel déclenché ne bloque plus la boucle de frames : la frame suivante est traitée sans attendre la synthèse (fake TTS lent → boucle non retardée).
- [ ] Une exception dans la synthèse backchannel est loggée et n'affecte jamais le turn en cours.
- [ ] Tests sur comportement externe : ordre/timing des événements, pas d'inspection d'état interne.

## Blocked by

None - can start immediately
