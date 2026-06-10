# 0117 — TurnLatencyMetrics : décomposition chronométrée du turn

## Parent

`prd/0018-oral-latency-reliability.md` (Module 1)

## What to build

Un collecteur de métriques par turn avec une interface minimale : `mark(turn_id, stage)` pour horodater les étapes du chemin critique (`endpoint`, `stt_finalized`, `loops_frozen`, `gate_decided`, `llm_first_token`, `tts_first_chunk`, `audio_first_byte`), `count(turn_id, counter)` pour les compteurs (draft adopté/jeté, retry de validation), et une projection : résumé par turn + agrégats glissants P50/P95 par étape. In-memory, rétention bornée, aucune persistance.

Instrumenter les sites d'émission existants du pipeline voix et de l'orchestrateur pour poser ces marks, et émettre le résumé par turn sur le canal debug-event existant à la fin du turn — visible dans la Debug View sans nouveau travail UI. Cette slice établit la **baseline** : toutes les optimisations suivantes du PRD s'évaluent en delta sur ces chiffres.

## Acceptance criteria

- [ ] Un turn vocal complet produit un résumé chronométré (durée de chaque étape du chemin critique) émis comme debug event à la fin du turn.
- [ ] Le taux d'adoption du draft spéculatif et le nombre de retries de validation apparaissent dans le résumé.
- [ ] Les agrégats P50/P95 par étape sont consultables (debug event périodique ou à la demande).
- [ ] Rétention bornée : les données des vieux turns sont évincées ; pas de croissance mémoire sur session longue.
- [ ] `mark`/`count` sur un turn_id inconnu est un no-op sûr (pas d'exception).
- [ ] Tests : fake clock → résumés et percentiles corrects ; rétention bornée vérifiée ; no-op sur id inconnu.

## Blocked by

None - can start immediately
