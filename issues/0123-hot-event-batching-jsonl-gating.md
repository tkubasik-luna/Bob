# 0123 — Batch des événements chauds + gating du sink JSONL

## Parent

`prd/0018-oral-latency-reliability.md` (Module 4, partiel)

## What to build

Réduire le coût du chemin chaud d'événements :

1. **Batching backend** : les événements haute fréquence (`speech_delta`, `reasoning_delta`, progression audio) sont coalescés côté backend sur une fenêtre de ~50–100 ms (setting) avant émission WS ; les événements basse fréquence restent immédiats. Le throttling rAF frontend existant (issue 0073) reste en place comme seconde ligne.
2. **Gating du sink JSONL debug** : `emit_debug` n'écrit sur disque que si le log est activé **et** qu'un consommateur existe ; les écritures sont batchées (flush périodique) au lieu d'un write+flush par événement.
3. **Rétention sans re-sérialisation** : la taille d'un événement est mesurée une fois à l'append et cachée ; l'application de la rétention ne re-dump plus le JSON.

## Acceptance criteria

- [ ] Une rafale de deltas token-par-token produit un nombre borné d'émissions WS par fenêtre (vérifié par capture d'événements sous fake clock).
- [ ] Les événements basse fréquence (assistant_msg, task_updated, audio_start/end) ne subissent aucun délai de batching.
- [ ] Aucune écriture JSONL quand la Debug View est fermée et le log fichier désactivé.
- [ ] Avec le log activé, les événements sont bien persistés (batchés) et l'ordre est préservé.
- [ ] L'application de la rétention ne re-sérialise plus chaque événement du buffer.
- [ ] La fenêtre de batching est un setting.
- [ ] Tests : volumes d'émission, gating du sink, intégrité de l'ordre — comportement externe uniquement.

## Blocked by

- `issues/0122-ws-fanout-timeout-eviction.md`
