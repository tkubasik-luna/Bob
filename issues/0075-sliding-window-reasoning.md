## Parent

prd/0011-agent-activity-feed.md

## What to build

Un bloc actif n'occupe pas tout l'écran même si le reasoning est long.

- **Frontend** : le `AgentBlock` actif affiche une **fenêtre glissante** des
  dernières lignes du reasoning (hauteur bornée, auto-scroll vers le bas au fil
  des deltas).
- Un dropdown / toggle « voir tout » déplie le reasoning complet du bloc actif.
- Le full texte reste accessible (dropdown live, et via le dépli une fois
  collapsé — cf. cycle de vie).

## Acceptance criteria

- [ ] Le bloc actif borne sa hauteur et auto-scroll sur les derniers tokens.
- [ ] Un long reasoning ne pousse pas les autres blocs hors écran.
- [ ] « Voir tout » affiche le reasoning complet du bloc.

## Blocked by

- issues/0069-reasoning-stream-tracer.md
