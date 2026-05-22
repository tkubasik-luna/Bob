## Parent

prd/0003-jarvis-orchestrator.md

## What to build

Drawer slide-in pour inspecter une sous-tâche, et mécanisme de dismiss pour les cards `done`/`failed`.

**Drawer** : click sur une card sidebar (hors bouton × et hors zone hover ×) ouvre un panneau slide-in depuis la droite (full height, ~50% width). Contenu : goal complet, transcript raw du sub-agent (tous les `task_messages` avec rôles user/assistant + actions colorées : progress=gris, ask_user=jaune, done=vert), result complet si done, reason si failed. Lecture seule. Bouton close ou clic outside pour fermer.

**Dismiss** : pour les cards en état `done`/`failed`, un bouton × secondaire (différent de cancel — celui-là c'est juste "cacher") permet de retirer la card de la sidebar SANS supprimer la row SQLite. Envoie `dismiss_task` WS event au backend pour persister le flag `dismissed=true`. Au reload, les dismissed ne sont pas re-renvoyés au frontend.

À la fin du slice : utilisateur peut consulter ce que ses sub-agents ont fait en détail, et nettoyer la sidebar des tâches terminées sans perdre l'historique.

## Acceptance criteria

- [ ] Table `tasks` ajoute colonne `dismissed BOOLEAN DEFAULT 0` via migration additionnelle (idempotente).
- [ ] WS client-to-server event `dismiss_task` `{task_id}` ; backend set `dismissed=true`.
- [ ] Backend ne renvoie pas les tasks `dismissed=true` au reconnect WS (filter `WHERE dismissed = 0`).
- [ ] `TaskCard` en état `done`/`failed` affiche bouton × dismiss (icon différent du × de cancel : ex. eye-off ou trash light).
- [ ] Click card (hors boutons) → ouvre `TaskDrawer`.
- [ ] `TaskDrawer` : slide-in animation, affiche title, goal, transcript task_messages (avec timestamps et action badges), result/reason, bouton close.
- [ ] Drawer responsive : prend ~50% width sur desktop, full screen sur mobile.
- [ ] `TaskDrawer` se met à jour en live si la task reçoit un nouvel event (cas d'ouverture pendant `running`).
- [ ] Smoke test manuel documenté : spawn task, attendre done, dismiss, vérifier card disparait ; re-spawn, ouvrir drawer pendant running, observer messages live.

## Blocked by

- issues/0019-sidebar-ui-shell-ws-events.md

## Manual smoke test

1. Start backend + frontend (`uv run uvicorn bob.main:app --reload` and
   `pnpm dev` from the frontend). Open the desktop app.
2. Ask Jarvis to spawn a long-ish sub-task ("draft 3 thank-you emails…").
   A card appears in the right sidebar with the blue "running" dot.
3. While the sub-task is `running`, click anywhere on the card body. The
   drawer slides in from the right (~50% width). Verify:
   - The title + state badge ("En cours") show at the top.
   - The "Objectif" section displays the goal.
   - The "Historique" section initially loads via the snapshot fetch,
     then appends new entries live as the sub-agent emits them
     (`task_message` events). Each row shows role, optional action
     badge, and a HH:MM:SS timestamp.
4. When the sub-agent finishes, the drawer still open: a "done" row is
   appended to the transcript, the state badge flips to "Terminée", and
   the "Résultat" section appears with the final payload.
5. Close the drawer (close button OR backdrop click OR ESC).
6. On the card (now `done`), hover to reveal the secondary × (eye-off
   icon). Click it. The card disappears from the sidebar.
7. Reload the desktop app (close + reopen window). The dismissed card
   does NOT reappear in the sidebar. Inspect `bob.db` to confirm the row
   is still in `tasks` with `dismissed = 1`.

### Known limitations

- The drawer does not have a polished slide-in CSS animation — it
  appears in place. Functional, but a follow-up could add a Tailwind
  transition.
