# 0124 — TaskSupervisor : zéro tâche de fond silencieuse

## Parent

`prd/0018-oral-latency-reliability.md` (Module 5, partiel)

## What to build

Un helper unique de supervision des tâches fire-and-forget : il attache un done-callback qui lit le résultat de la tâche, logge toute exception avec contexte (nom, session, turn/msg id), et émet optionnellement un événement client/debug. Adoption sur tous les sites critiques actuels :

- synthèse TTS (proactive et turn principal) — une synthèse qui échoue émet un événement visible au lieu de produire un « audio fantôme » ;
- dispatch des subscribers du bus d'événements interne — un handler qui crash est loggé avec le topic ;
- flusher proactif et typing-reset de l'orchestrateur — un flusher mort est détecté.

Inclut aussi deux observabilités proches : l'échec de persistance d'un turn voix émet un événement client (`voice_persist_failed`), et l'échec du startup MCP est enregistré dans l'état applicatif et exposé via l'endpoint de santé.

## Acceptance criteria

- [ ] Une tâche supervisée qui lève produit un log avec contexte + un événement debug ; le résultat de la tâche est toujours consommé (aucun « Task exception was never retrieved »).
- [ ] Une synthèse TTS proactive qui échoue émet un événement visible côté client/debug.
- [ ] Un subscriber du bus d'événements qui lève est loggé avec son topic ; les autres subscribers reçoivent l'événement.
- [ ] Un crash du flusher proactif est détecté (log + événement), pas silencieux.
- [ ] Un échec de persistance de turn voix émet `voice_persist_failed` vers le client.
- [ ] Un échec de startup MCP est visible dans la réponse de l'endpoint de santé.
- [ ] Tests : tâches qui lèvent à chaque site adopté → log + événement observés ; comportement externe uniquement.

## Blocked by

None - can start immediately
