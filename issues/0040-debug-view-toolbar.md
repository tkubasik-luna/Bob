## Parent

prd/0005-debug-view.md

## What to build

Ajouter la toolbar de filtres à la vue debug et le coloring sévérité / catégorie sur chaque ligne, pour que l'utilisateur puisse focaliser sa lecture.

Périmètre frontend uniquement (aucun changement backend) :

- Nouveau composant `frontend/src/components/debug/DebugToolbar.tsx` rendu en haut de `DebugView`. Contient :
  - 7 chips cliquables, un par catégorie (`input`, `llm`, `decision`, `task`, `output`, `voice`, `system`). Chaque chip a sa propre couleur (palette à définir au moment de l'impl, ex: input=bleu, llm=violet, decision=cyan, task=vert, output=jaune, voice=rose, system=gris). État `on/off` toggle au click. Visuellement `off` = chip grisé / réduit en opacité.
  - 1 dropdown `<select>` pour le seuil severity, 5 options (`trace`, `debug`, `info`, `warn`, `error`). Affiche tout event ayant `severity >= seuil`. Défaut `info`.
- Étendre `useDebugWs.ts` (ou l'état local de `DebugView`) pour stocker les filtres : `{categoriesOn: Set<category>, severityThreshold: severity}`. Défaut : toutes catégories ON, seuil `info`.
- Filtrage côté frontend : `DebugView` calcule `filteredEvents = events.filter(...)` à partir de `events` + filtres. Le filtrage doit être pure et rapide (Set + ordre des severities en map d'index pour comparer).
- Coloring des lignes dans le feed :
  - Chaque ligne porte une classe / style basé sur sa `severity` : `warn` → texte ambre (`--warn` de hud.css ou variable dédiée `--debug-warn`), `error` → texte rouge (`--err` / `--debug-error`), `trace` → texte gris désaturé, `debug` / `info` → texte neutre.
  - Chaque ligne affiche un chip de catégorie à gauche du summary (juste après le timestamp), coloré selon la palette catégorie. Mini-pill avec texte court (ex: `INPUT`, `LLM`, `DECISION`, `TASK`, `OUT`, `VOICE`, `SYS`).
- Style : utiliser JetBrains Mono (déjà chargée), aligner le timestamp en colonne fixe (ex: 14 chars) pour faciliter la lecture, taille de police compact (~12-13px).
- Background sombre uniforme sur toute la fenêtre debug. Réutiliser `--hud-bg` ou définir `--debug-bg` distincte si la teinte HUD n'est pas adaptée à un terminal.
- Pas de persistance des filtres en localStorage en v1 — chaque ouverture de session app repart sur les défauts.

## Acceptance criteria

- [ ] La fenêtre debug affiche une toolbar en haut avec 7 chips de catégorie et un dropdown severity.
- [ ] À l'ouverture, toutes les catégories sont ON et le seuil est `info` — le feed montre tous les events sauf `trace` et `debug`.
- [ ] Click sur un chip catégorie le bascule en `off` — toutes les lignes de cette catégorie disparaissent du feed instantanément.
- [ ] Click à nouveau sur le chip le réactive — les lignes réapparaissent (toujours dans l'ordre chronologique).
- [ ] Changer le dropdown severity à `warn` masque tous les events `info`/`debug`/`trace`, ne montre que `warn` et `error`.
- [ ] Changer le dropdown à `trace` montre TOUS les events (incluant les `trace` haute fréquence comme audio chunks si présents).
- [ ] Chaque ligne du feed affiche `[HH:MM:SS.mmm] [chip-cat] summary` avec le chip catégorie coloré selon la palette.
- [ ] Une ligne de severity `warn` a un texte de couleur ambre/jaune ; une de severity `error` est rouge ; une de severity `trace` est gris désaturé.
- [ ] Toute la UI debug utilise JetBrains Mono.
- [ ] Le background est sombre uniforme (pas blanc/clair).
- [ ] Aucune régression sur le flow events / shortcut / fenêtre depuis slice 0038-0039.

## Blocked by

issues/0039-debug-view-instrumentation.md
