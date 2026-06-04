## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Remplacer l'orb placeholder de slot-core par l'orb **conscience** nebula porté (shader WebGL + couche « life » + wrapper, depuis `Design Mockup/conscience-shader.js`, `conscience-life.js`, `conscience.jsx` et `p3d-core.jsx` CoreNebula), teinté **nacre** rose/lavande.

Introduire un reducer **pur** `orbState` qui dérive `{ state, energy }` depuis l'état réel (chat + tâches), et l'alimenter à l'orb : l'orb respire au repos et change d'humeur (`idle/listen/think/speak/alert/error`) selon ce que fait Bob. La voix (TTS RMS) module l'orb pendant la lecture. Le tuning des paramètres orb (motion/glow/mood/variant) reste exposé derrière `?dev`.

## Acceptance criteria

- [ ] L'orb conscience nebula (nacre) rend dans slot-core, fidèle à `p3d-core.jsx` CoreNebula + screenshots `Design Mockup/screenshots/01-piste.png` et `p3d-default.png` ; label `CORE · conscience` dessous.
- [ ] Reducer `orbState(chat, tasks) → {state, energy}` est un module pur, sans dépendance UI.
- [ ] L'orb respire au repos ; passe en think / speak / delegate selon l'activité réelle (réflexion, tâches en cours, réponse en streaming) ; alerte/erreur sur échec.
- [ ] La voix TTS module l'orb pendant la lecture.
- [ ] Tuning orb accessible en `?dev` (réutilise l'infra de tweaks existante).
- [ ] Tests unitaires `orbState` : repos / réflexion / délégation (tâches en cours) / réponse streaming / erreur → `{state, energy}` attendus. Prior art : tests de dérivation d'état orb existants.

## Blocked by

- issues/0083-piste3d-foundation-shell-css.md
