## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Remplacer le picker de provider haut-gauche par le modal **RÉGLAGES** de la maquette : un bouton gear haut-droite ouvre un panneau avec un segmented control Claude CLI ↔ LM Studio, l'URL serveur + presets + état de joignabilité, une liste de modèles locaux (nom / params / quant / RAM) avec sélection, et un slider de longueur de contexte.

Câbler sur les endpoints `/api/llm/*` existants (`GET selection`, `GET models`, `PUT selection`) et conserver la persistance de la sélection.

## Acceptance criteria

- [ ] Bouton gear `RÉGLAGES` haut-droite ouvre un modal, fidèle à `Design Mockup/p3d-panels.jsx` SettingsControl + screenshot `p3d-settings.png`.
- [ ] Segmented Claude CLI ↔ LM Studio ; Claude affiche l'état connecté + modèle fixe.
- [ ] LM Studio : champ URL + presets + état joignable/hors-ligne, câblé `PUT /api/llm/selection`.
- [ ] Liste de modèles (nom / params / quant / RAM) depuis `GET /api/llm/models`, sélection câblée (`PUT`).
- [ ] Slider de longueur de contexte (feature 0013) conservé et câblé.
- [ ] Les choix moteur/modèle persistent entre les lancements.
- [ ] L'ancien ProviderPicker haut-gauche est retiré.

## Blocked by

- issues/0083-piste3d-foundation-shell-css.md
