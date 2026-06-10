# 0127 — Runner : stall guard réactif

## Parent

`prd/0018-oral-latency-reliability.md` (Module 7, partiel)

## What to build

Rendre la détection d'impasse du sub-agent runner plus réactive : le compteur de stall se réinitialise quand le **code d'erreur de l'outil change** (une nouvelle erreur = un vrai changement de diagnostic), pas seulement sur un résultat réussi ; et le seuil de terminaison forcée passe de 4 à 3 itérations consécutives sans progrès (en setting). Effet utilisateur : un outil qui échoue en boucle est coupé et rapporté en ~15 s au lieu de 30 s+ de silence.

## Acceptance criteria

- [ ] Un outil qui échoue avec le **même** code d'erreur n'empêche plus la terminaison forcée : la run se termine au seuil au lieu de courir jusqu'au cap d'itérations.
- [ ] Un changement de code d'erreur entre deux tentatives réinitialise le compteur (le modèle essaie réellement autre chose).
- [ ] Le seuil de force est un setting, défaut 3.
- [ ] La terminaison forcée produit un `done(failed, stalled)` propre avec activité projetée (l'utilisateur a un feedback).
- [ ] Tests : séquences d'erreurs identiques vs changeantes sous fake LLM/outils → terminaison au bon moment, vérifiée par les événements émis.

## Blocked by

None - can start immediately
