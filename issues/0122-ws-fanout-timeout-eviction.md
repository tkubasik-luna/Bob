# 0122 — Fan-out WS : timeout par emitter + éviction

## Parent

`prd/0018-oral-latency-reliability.md` (Module 4, partiel)

## What to build

Blinder la boucle de broadcast du bus d'événements : chaque `await emitter(payload)` est borné par un timeout configurable (~1–2 s par défaut, en setting). Un emitter qui timeout ou lève est **évincé immédiatement** du registre — il ne reçoit plus rien et ne peut plus bloquer les autres. Les événements suivants continuent vers les emitters sains. Couvre aussi le nettoyage des références mortes du set d'emitters (audit 4.1, 5.9) : une fenêtre HUD ou debug zombie ne gèle plus jamais l'orchestrateur.

## Acceptance criteria

- [ ] Un emitter qui ne répond jamais (hang simulé) est évincé dans le délai du timeout ; les événements suivants atteignent les emitters sains sans délai ajouté.
- [ ] Un emitter qui lève une exception est évincé après son premier échec ; le set d'emitters ne garde aucune référence morte.
- [ ] Le timeout est un setting avec défaut 1–2 s.
- [ ] L'éviction est loggée avec contexte (quel emitter, pourquoi).
- [ ] Aucune régression sur le chemin nominal : deux fenêtres saines reçoivent tous les événements dans l'ordre.
- [ ] Tests : emitter pendu, emitter qui throw, emitters sains concurrents — vérifiés par événements reçus, sous fake clock.

## Blocked by

None - can start immediately
