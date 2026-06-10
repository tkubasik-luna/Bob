# 0128 — Prefix de prompt stable : cache catalogue tools + ordre des fragments

## Parent

`prd/0018-oral-latency-reliability.md` (Module 7, partiel)

## What to build

Stabiliser le préfixe de prompt pour exploiter le prefix-cache des modèles locaux :

1. **Runner** : la sélection des outils annoncés (`select_tools`) et le rendu du catalogue (JSON Schema, ~20 KB) sont calculés **une fois par run** (le goal est immuable) et mis en cache ; chaque itération réutilise le bloc rendu identique.
2. **Assemblage de prompt (Jarvis + runner)** : les fragments stables (system block, catalogue d'outils) sont ordonnés en tête, les fragments variables (contexte temporel, feedback de validation, état Thinker) en queue — le préfixe reste identique entre les turns/itérations, donc le KV-cache de LM Studio évite le re-prefill complet.

## Acceptance criteria

- [ ] Sur une run de sub-agent multi-itérations, le bloc catalogue d'outils du prompt est byte-identique à chaque itération (`select_tools` calculé une seule fois).
- [ ] Le préfixe du prompt système Jarvis est byte-identique entre deux turns consécutifs d'une même session (seuls les fragments de queue varient).
- [ ] Le feedback de validation injecté lors d'un retry n'altère pas le préfixe stable (il arrive après).
- [ ] Aucune régression fonctionnelle : les outils annoncés et le contexte temporel restent présents et corrects dans le prompt final.
- [ ] Tests : stabilité du contenu de prompt observée entre itérations/turns (comparaison de contenu, pas comptage d'appels internes).

## Blocked by

None - can start immediately
