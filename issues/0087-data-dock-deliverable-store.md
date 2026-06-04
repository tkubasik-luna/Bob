## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Rendre le dock **DONNÉES GÉNÉRÉES** dans slot-data.

Introduire un `deliverableStore` scope-session qui collecte les deliverables générés (depuis `task_result.result_payload` + `ui_payload` de Bob) avec état `fresh`/`seen` et **sans éviction automatique**. Introduire une projection **pure** `deliverableCard` (`toCard(deliverable, task) → {title, sub, type, sections}`, 1 carte par deliverable, icône type dominant / glyph composite si mixte). Les cartes atterrissent animées (highlight `fresh`), persistent toute la session, et un compteur d'actives s'affiche.

Le clic sur une carte ouvre l'overlay **existant** pour l'instant (re-skinné en 0088) — la slice reste pleinement fonctionnelle.

## Acceptance criteria

- [ ] Dock DONNÉES GÉNÉRÉES rend dans slot-data avec compteur d'artefacts actifs, fidèle à `Design Mockup/p3d-panels.jsx` DataField + screenshot `p3d-default.png`.
- [ ] `deliverableStore` scope-session : ajoute depuis `task_result.result_payload` + `ui_payload` ; état `fresh` → `seen` ; pas d'éviction automatique (pas de TTL).
- [ ] Projection `deliverableCard` pure : 1 carte / deliverable, `title` = Task.title, `sub` = goal/résumé, icône = type dominant (glyph composite si sections hétérogènes).
- [ ] Une nouvelle carte s'anime (`fresh`) à l'arrivée ; persiste toute la session.
- [ ] Clic carte → ouvre l'artefact (overlay existant pour l'instant) → `fresh` passe `seen`.
- [ ] Tests `deliverableCard` : mail mono-type / multi-mail / composite hétérogène / `ui_payload` de Bob → title/sub/type/sections attendus. Prior art : test d'heuristique d'overlay existant.

## Blocked by

- issues/0083-piste3d-foundation-shell-css.md
