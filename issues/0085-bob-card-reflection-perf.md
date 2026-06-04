## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Rendre la carte **BOB** dans slot-task comme carte de front du (futur) deck : prompt (dernier message utilisateur) → **Réflexion** (`reasoning_delta` du thread principal en streaming, avec fallback narré quand aucun reasoning n'est streamé) → **Tâches en arrière-plan** (liste live des sous-tâches invoquées : nom + outil + état/rendu) → **Réponse** (synthèse réelle streamée, markdown) → **footer perf** (tok/s · ttft · ctx, en phase `done`).

Introduire un module **pur** `reflectionNarrator` pour le fallback. Garantir côté backend que le thread principal Bob émet ses `reasoning_delta` + `perf` avec un `agent_ref` stable, pour que la carte s'y bind comme les sous-tâches au leur (ajouter si absent). Réutiliser le rendu markdown existant pour la réponse. Remplace le rôle du panneau d'activité agent pour le fil de Bob. (Les cartes sous-tâches + l'empilement viennent dans 0086.)

## Acceptance criteria

- [ ] Carte BOB rend prompt / Réflexion / Tâches en arrière-plan / Réponse / perf, fidèle à `Design Mockup/p3d-panels.jsx` BobBody + screenshots `p3d-settings.png` / `01-piste.png`.
- [ ] Réflexion streame le `reasoning_delta` réel du thread principal quand dispo ; sinon ligne narrée par `reflectionNarrator`.
- [ ] La liste « Tâches en arrière-plan » reflète les sous-tâches réelles (nom + outil + état en cours / ✓ rendu) en live.
- [ ] Réponse streame la synthèse réelle en markdown (rendu markdown existant réutilisé).
- [ ] Footer perf affiche tok/s · ttft · ctx réels en phase `done`.
- [ ] Backend : le thread principal Bob émet reasoning/perf avec un `agent_ref` stable (vérifié via WS/debug) ; ajout si absent.
- [ ] Question simple sans délégation → carte BOB sans section « tâches » (fil épuré).
- [ ] Synthèse proactive de Bob → carte BOB s'affiche même sans prompt.
- [ ] Tests `reflectionNarrator` : reasoning présent (prime) / absent (narration dérivée) / events partiels. Prior art : test de phase d'agent existant.

## Blocked by

- issues/0083-piste3d-foundation-shell-css.md
