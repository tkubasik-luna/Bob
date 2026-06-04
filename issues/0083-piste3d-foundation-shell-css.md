## Parent

prd/0014-hud-piste-3d-nacre.md

## What to build

Monter le squelette de la scène « Piste 3D · Nacre » end-to-end dans la fenêtre HUD `new`, en gardant l'app fonctionnelle. Un conteneur racine `piste` (classes layout depth / camera deep / panel frost), des couches fond + grain, un stage 3D avec trois emplacements vides (core / task / data), et le bloc identité haut-gauche `● BOB · {état}` + tagline.

Porter les parties fondamentales de `Design Mockup/p3d.css` (tokens, base, layout, camera, stage, identité, fond/grain) dans une feuille de style **scopée**, en réconciliant les sélecteurs qui entrent en collision avec la feuille HUD existante (`ov-*`, `overlay-*`, `panel*`, `md-*`) : les règles overlay du mockup supersèdent (même rôle), les sélecteurs génériques sont préfixés/scopés.

L'orb actuel reste en **placeholder** dans slot-core ; input / transcript / mute restent fonctionnels (placement provisoire). Le mot d'état de l'identité reflète l'état orb courant (réutilise la dérivation existante pour l'instant).

C'est le seul gate partagé : il débloque l'orb, le deck, le dock et les réglages.

## Acceptance criteria

- [ ] La fenêtre `new` rend le conteneur `piste` (layout depth / cam deep / panel frost) avec fond `#160F18` + grain, fidèle à `Design Mockup/p3d.css` et screenshot `Design Mockup/screenshots/p3d-default.png`.
- [ ] Identité haut-gauche `● BOB · {état}` + tagline « nacre — sphère liquide · sanctuaire en profondeur » présente et stylée comme la maquette.
- [ ] Les 3 emplacements stage (core / task / data) existent et sont positionnés en profondeur ; core contient l'orb placeholder, task/data vides.
- [ ] `p3d.css` (tokens/base/layout/camera/stage/identité/fond) porté dans une feuille scopée ; aucun style HUD existant cassé.
- [ ] Collisions de sélecteurs (`ov-*`, `overlay-*`, `panel*`, `md-*`) réconciliées et documentées dans le diff.
- [ ] Input + transcript + mute toujours fonctionnels (placement provisoire accepté).
- [ ] Fenêtres `legacy` (ChatView) et `debug` inchangées.

## Blocked by

None - can start immediately.
