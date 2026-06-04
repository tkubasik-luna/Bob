## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Re-skinner l'overlay plein écran au chrome de la maquette (coins d'angle, faisceau, en-tête mono `BOB · GÉNÉRÉ` + chip + `RÉF · xxx`, corps typé, pied d'actions), ouvert au clic d'une carte du dock.

Conserver le comportement **stack de sections** (deliverable composite → stack — feature 0011 préservée). Porter les surfaces typées **Mail** et **Document** depuis `Design Mockup/p3d-overlay.jsx` (Document réutilise le rendu markdown existant). Pied d'actions : **LIRE À VOIX HAUTE** (câblé TTS), **OUVRIR**, **FERMER**. Retirer l'auto-ouverture actuelle — l'overlay ne s'ouvre qu'au clic. Fermeture via Échap / ✕ / FERMER / clic hors carte.

## Acceptance criteria

- [ ] Overlay rend le chrome maquette (coins, faisceau, header mono `BOB · GÉNÉRÉ` + chip + `RÉF · xxx`, footer), fidèle à `Design Mockup/p3d-overlay.jsx` + `p3d.css` (`ov-*`).
- [ ] Surfaces typées **Mail** + **Document** portées (Document réutilise le rendu markdown existant), fidèles à MailSurface / DocSurface.
- [ ] Deliverable composite → stack de sections dans l'overlay (comportement 0011 préservé).
- [ ] Footer : LIRE À VOIX HAUTE → TTS, OUVRIR, FERMER.
- [ ] Auto-open supprimé : l'overlay s'ouvre uniquement au clic d'une carte du dock.
- [ ] Fermeture via Échap / ✕ / FERMER / clic hors carte.
- [ ] Sélecteurs `ov-*` réconciliés avec l'ancien overlay (les règles maquette supersèdent).

## Blocked by

- issues/0087-data-dock-deliverable-store.md
