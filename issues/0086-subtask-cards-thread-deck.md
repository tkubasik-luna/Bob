## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Ajouter les **cartes sous-tâches** derrière la carte BOB et transformer slot-task en **deck 3D empilé** complet.

Introduire un modèle **pur** `threadDeck` qui ordonne toutes les cartes (Bob + sous-tâches), assigne rang → transform (translate / scale / rotateZ jitter, opacité, z-index), sélectionne la carte de front (auto = la plus récemment active, ou la carte épinglée), et expose le promote-au-clic avec maintien temporel. Les SubCards rendent chaque sous-tâche réelle (réflexion → appel d'outil avec args/résultat → rendu ↳), chrome lavande, glyph ◇, mention « par BOB ». La carte vivante glisse au front ; cliquer une carte arrière la promeut. La carte BOB affiche le débordement `+N tâches`.

## Acceptance criteria

- [ ] `threadDeck(bob, subs, pinned) → cartes ordonnées` est un module pur (rank / transform / front / promote), sans dépendance UI.
- [ ] SubCards rendent les sous-tâches réelles (réflexion / outil name+args+résultat / rendu ↳), teinte lavande + ◇ + « par BOB », fidèle à `Design Mockup/p3d-panels.jsx` SubBody/DeckCard.
- [ ] Deck empilé 3D : transforms par rang (translate / scale / rotateZ jitter), la carte vivante glisse au front (timing réel), fidèle à screenshots `01-piste.png` / `p3d-settings.png`.
- [ ] Clic sur carte arrière → promotion au front (pin temporel) ; ordre DOM stable entre reshuffles.
- [ ] Carte BOB affiche `+N tâches` quand des cartes sont empilées derrière.
- [ ] Fallback `nom d'outil + état` si args/résultat des tool_call sont redacted.
- [ ] Tests `threadDeck` : Bob seul / Bob + N subs / sélection front auto / promote par pin / stabilité de l'ordre DOM. Prior art : utilitaires purs existants.

## Blocked by

- issues/0085-bob-card-reflection-perf.md
