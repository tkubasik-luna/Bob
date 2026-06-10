# 0125 — FSM force-reset après exception + fix race voice_start

## Parent

`prd/0018-oral-latency-reliability.md` (Module 5, partiel)

## What to build

Deux invariants de la machine à états vocale :

1. **Force-reset** : si le say-path lève une exception à n'importe quelle étape (avant le premier audio, en plein streaming, ou pendant la finalisation elle-même), la FSM revient toujours à l'état idle — l'invariant « jamais deux turns en bob_speaking » survit à tout chemin d'exception. Le reset est défensif : même si `_finalize_say` échoue (WS fermée), un hard-reset s'applique.
2. **Race voice_start** : le handler de `voice_start` vide le slot de loop de la session **avant** d'arrêter l'ancienne loop, et l'arrêt s'exécute sous suppression d'exception — deux `voice_start` rapprochés laissent toujours exactement une loop vivante.

## Acceptance criteria

- [ ] Exception injectée dans le say-path à chaque étape (avant premier audio / mi-streaming / pendant finalize) → la FSM est idle après, et un `voice_start` suivant fonctionne normalement.
- [ ] Une exception dans la finalisation elle-même n'empêche pas le retour à idle.
- [ ] Deux `voice_start` rapides (le second pendant que le stop du premier échoue) → exactement une loop active, frames routées vers elle.
- [ ] Chaque force-reset est loggé + émet un événement debug (observabilité du chemin anormal).
- [ ] Tests : injection d'exceptions par étape via fakes, assertions sur l'état FSM exposé et les événements émis — prior art tests FSM du PRD 0016.

## Blocked by

- `issues/0118-endpoint-concurrent-commit.md`
