## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Orchestrer l'expérience de **démarrage à froid / repos** et le **fade-in** des panneaux. À froid (aucune conversation) : orb + identité + input centrés, deck et dock cachés/estompés avec une invitation discrète. Le deck et le dock apparaissent en fade dès qu'une première donnée réelle arrive. Le mouvement suit le timing réel des événements.

## Acceptance criteria

- [ ] À froid : orb + identité + input centrés ; deck (slot-task) et dock (slot-data) estompés/cachés + invitation discrète.
- [ ] Première donnée réelle (prompt / tâche / deliverable) → deck/dock apparaissent en fade.
- [ ] Transitions pilotées par le timing réel (pas de cadence artificielle), fidèles à l'esprit de la maquette.
- [ ] Retour à un état repos cohérent en fin de conversation/session.
- [ ] Aucune régression sur l'orb / la carte BOB / le dock déjà livrés.

## Blocked by

- issues/0084-conscience-orb-orbstate-reducer.md
- issues/0085-bob-card-reflection-perf.md
- issues/0087-data-dock-deliverable-store.md
