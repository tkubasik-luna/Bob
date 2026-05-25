## Parent

prd/0005-debug-view.md

## What to build

Finaliser le comportement de feed type `tail -f` : auto-scroll en bas, pause auto si l'utilisateur scroll up, badge pour reprendre, plus boutons pause/clear dans la toolbar et raccourci Space pour pause global.

Périmètre frontend uniquement :

- Dans `DebugView` (ou un sous-composant `DebugFeed`), implémenter la logique d'auto-scroll :
  - Garder une ref au conteneur scrollable du feed.
  - À chaque nouvel event arrivé, si `isAtBottom` (scrollTop ~ scrollHeight - clientHeight, avec tolérance de quelques px), scroll automatiquement vers le bottom.
  - Si l'utilisateur a scrollé up (`isAtBottom = false`), ne PAS auto-scroller. Incrémenter un compteur `newEventsSinceScroll` à chaque nouvel event.
  - Quand `newEventsSinceScroll > 0`, afficher un badge flottant en bas du feed : `↓ N nouveaux events`. Click → scroll au bottom et reset le compteur.
  - Reset automatique de `newEventsSinceScroll` à 0 dès que l'utilisateur revient au bottom manuellement.
- Ajouter à `DebugToolbar` (issue 0040) deux nouveaux boutons :
  - **Pause/Resume** : toggle l'état `paused` dans `useDebugWs`. Label change selon état (`⏸ Pause` / `▶ Resume`). Visuellement marqué comme actif quand `paused = true`.
  - **Clear** : vide la liste locale `events` côté frontend uniquement (sans toucher au ring buffer backend). Le label affiche un compteur tel que `Clear (N)` montrant le nombre d'events visibles si utile, ou simplement `Clear`.
- Quand `paused = true` :
  - Les events continuent d'arriver via la WS mais sont buffered dans un `pendingEvents` séparé.
  - Le feed visible reste figé (pas de nouvelles lignes).
  - Au resume, les `pendingEvents` sont mergés dans `events` dans l'ordre chronologique d'arrivée.
- Raccourci `Space` sur la fenêtre debug = toggle `paused`. À installer comme `keydown` listener sur `document` quand `DebugView` est mount, ignorer si focus sur input/textarea/contenteditable (peu probable dans la debug window, mais robust).
- Vérifier que les filtres (slice 0040) et l'expand (slice 0041) continuent de marcher correctement avec l'auto-scroll : si la ligne expand pousse le contenu vers le bas, l'auto-scroll doit suivre (ou pas, selon si on était au bottom). Comportement attendu : un click expand ne déclenche PAS un auto-scroll, mais le prochain event arrivé en auto-scroll respecte `isAtBottom` après l'expand.
- S'assurer que les polices, padding, layout général sont propres : ligne compacte (~22-26px de haut) pour pouvoir voir beaucoup d'events à l'écran d'un coup.

## Acceptance criteria

- [ ] À l'ouverture, le feed est scrollé au bottom, auto-scroll actif.
- [ ] Quand un nouvel event arrive et que je suis au bottom, le feed défile automatiquement pour le montrer.
- [ ] Quand je scroll up de plus de quelques px, l'auto-scroll se met en pause silencieusement (pas de message).
- [ ] Quand un nouvel event arrive alors que j'ai scrollé up, un badge flottant `↓ N nouveaux events` apparaît en bas du feed. Le compteur incrémente avec chaque nouvel event.
- [ ] Click sur le badge me scroll au bottom et fait disparaître le badge.
- [ ] Scroll manuel au bottom (sans cliquer le badge) fait aussi disparaître le badge et réactive l'auto-scroll.
- [ ] Le bouton `Pause` dans la toolbar fige le feed visible : les events continuent d'arriver côté WS mais ne s'affichent plus.
- [ ] Click sur `Resume` débloque le feed et insère tous les events arrivés pendant la pause dans l'ordre chronologique.
- [ ] La touche `Space` (dans la fenêtre debug) toggle pause/resume comme le bouton.
- [ ] Le bouton `Clear` vide le feed local instantanément. Les futurs events s'affichent normalement à partir de là.
- [ ] Le ring buffer backend n'est PAS touché par `Clear` : si je hide puis re-show la debug window (Cmd+Shift+D off/on), les events sont à nouveau replayés depuis le backend.
- [ ] Le layout général est compact et lisible : chaque ligne fait ~22-26px de haut, le timestamp est aligné en colonne fixe, la font est JetBrains Mono.
- [ ] Aucune régression sur les filtres catégorie/severity (slice 0040) ni sur l'expand inline et le highlight turn_id (slice 0041).

## Blocked by

issues/0040-debug-view-toolbar.md
