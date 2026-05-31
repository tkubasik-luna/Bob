## Parent

prd/0011-agent-activity-feed.md

## What to build

Le feed garde l'historique de la session et survit au reload ; le `TaskDrawer`
est retiré, son rôle étant absorbé par le bloc expand.

- **Rétention** : tous les blocs (actifs + collapsés) restent empilés et
  scrollables tant que l'app tourne (mémoire bornée par session).
- **Rehydrate** : au reload, `activityFeedStore` reconstruit les blocs terminés
  depuis le snapshot `TaskStore` persisté — état + résumé + résultat. Le
  reasoning live d'une task déjà terminée n'est PAS re-streamé.
- **Suppression du `TaskDrawer`** et de son flux
  `request_task_messages` / `task_messages_snapshot` s'il n'est plus consommé.
  Tout le détail (objectif, réflexion, chips, résultat) vit dans le bloc expand.

## Acceptance criteria

- [ ] Le feed conserve toute la session en scrollback.
- [ ] Au reload, les blocs terminés sont reconstruits (état/résumé/résultat) sans
      reasoning live.
- [ ] Le `TaskDrawer` est supprimé ; aucune régression d'accès au détail/résultat
      (tout passe par le bloc expand + overlays).
- [ ] Tests store : rehydrate depuis un snapshot `TaskStore` reconstruit les
      blocs attendus.

## Blocked by

- issues/0074-block-lifecycle-collapse.md
- issues/0076-collapsable-side-panel.md
