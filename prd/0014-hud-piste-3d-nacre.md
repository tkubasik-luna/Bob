# 0014 — Refonte HUD « Piste 3D · Nacre »

> Aligner ultra-fidèlement la fenêtre HUD `new` de Bob sur le mockup **Piste 3D · Nacre**
> (`Design Mockup/Bob - Pistes 3D.html` + `p3d-*.jsx` + `p3d.css` + screenshots `p3d-*`),
> en réutilisant la couche données réelle (backend/WS) existante.

## Problem Statement

Le HUD actuel de Bob (fenêtre `new`) fonctionne — orb central, transcript/input en bas, rail
d'activité agent à droite, overlay de deliverables — mais il ne ressemble pas à la vision
produit. Le mockup « Piste 3D · Nacre » exprime une intention forte et finie : une **conscience**
centrale (orb nebula nacré) entourée d'un **fil de pensée** à gauche (Bob qui réfléchit puis
délègue à des sous-tâches) et d'une **mémoire d'artefacts générés** à droite, le tout dans un
langage visuel cohérent (profondeur 3D, verre dépoli, mono + grotesk, palette rose/lavande).

L'utilisateur veut que l'application **devienne** cette maquette, sans perdre les fonctions déjà
livrées (voix, picker LLM, tâches réelles, Gmail, debug). Aujourd'hui l'écart est important :
layout inversé (agent à gauche vs rail à droite), orb différent, pas de dock de données
persistant, settings au mauvais endroit et sous une autre forme.

## Solution

Refondre **in-place** la fenêtre HUD `new` pour qu'elle reproduise la piste « Nacre » du mockup,
en branchant chaque zone visuelle sur les événements WebSocket réels que le backend émet déjà.
La fenêtre `legacy` (ChatView) et la fenêtre `debug` restent intactes en fallback ; tout le
backend est réutilisé tel quel (à deux petits ajustements près).

Du point de vue de l'utilisateur, après la refonte :

- **Le centre** est une conscience vivante (orb nebula nacré) qui respire et change d'humeur
  selon ce que fait Bob (au repos / écoute / réflexion / réponse / alerte / erreur).
- **À gauche**, un **deck de cartes empilées en 3D** montre le fil de la conscience : la carte
  **BOB** (prompt → réflexion → tâches en arrière-plan → réponse → perf) devant, et derrière les
  **sous-tâches** qu'il a invoquées (réflexion → appel d'outil → rendu). La carte vivante glisse
  au premier plan ; on peut cliquer une carte arrière pour la promouvoir.
- **À droite**, un dock **DONNÉES GÉNÉRÉES** accumule en cartes les artefacts que Bob produit
  (courriels, documents…), qui persistent pour la session. Un clic ouvre l'artefact en plein
  écran (overlay typé), avec lecture à voix haute.
- **En haut à gauche**, l'identité `● BOB · {état}` + la tagline de la piste.
- **En haut à droite**, un bouton **RÉGLAGES** ouvre un modal pour basculer Claude CLI ↔ LM Studio,
  choisir/charger un modèle local et régler la longueur de contexte.
- **En bas au centre**, un champ de saisie minimal toujours visible + une ligne de transcription
  (caption voix). Bouton **mute** en bas à droite.

L'objectif de fidélité : porter le CSS et les composants du mockup **verbatim** autant que
possible (le mockup est déjà du React), et substituer aux données scriptées les vraies données.

## User Stories

### Conscience / orb central
1. En tant qu'utilisateur, je veux voir une conscience centrale (orb nebula nacré) au cœur du HUD, afin de sentir que Bob est une entité vivante et non un simple chat.
2. En tant qu'utilisateur, je veux que l'orb **respire** doucement au repos, afin de percevoir qu'il est présent même quand rien ne se passe.
3. En tant qu'utilisateur, je veux que l'orb change d'humeur selon l'état de Bob (réflexion, délégation, réponse), afin de lire d'un coup d'œil ce qu'il est en train de faire.
4. En tant qu'utilisateur, je veux que l'orb réagisse à la voix (TTS) en cours de lecture, afin de relier le son à la présence visuelle.
5. En tant qu'utilisateur, je veux un label discret `CORE · conscience` sous l'orb, afin de comprendre la métaphore.

### Fil de conscience (colonne gauche)
6. En tant qu'utilisateur, je veux voir ma demande (prompt) en tête de la carte BOB, afin de garder le contexte de ce que j'ai demandé.
7. En tant qu'utilisateur, je veux voir la **réflexion** (monologue) de Bob se streamer, afin de comprendre son raisonnement avant qu'il agisse.
8. En tant qu'utilisateur, quand mon modèle local ne produit pas de raisonnement, je veux une réflexion **narrée** dérivée des événements, afin de ne jamais voir une section vide.
9. En tant qu'utilisateur, je veux voir la liste des **tâches en arrière-plan** que Bob invoque, avec leur outil et leur état (en cours / rendu), afin de suivre la délégation.
10. En tant qu'utilisateur, je veux voir la **réponse** synthétisée de Bob se streamer en markdown, afin de lire le résultat final dans le fil.
11. En tant qu'utilisateur, je veux voir des **métriques de perf** (tok/s, ttft, ctx) quand Bob a fini, afin de juger la rapidité du moteur.
12. En tant qu'utilisateur, je veux voir chaque **sous-tâche** comme sa propre carte (réflexion → appel d'outil avec args/résultat → rendu), afin de comprendre comment chaque main travaille.
13. En tant qu'utilisateur, je veux que la carte vivante **glisse au premier plan** du deck, afin de toujours voir l'activité courante sans chercher.
14. En tant qu'utilisateur, je veux pouvoir **cliquer une carte en arrière** pour la promouvoir devant, afin de relire une tâche précise.
15. En tant qu'utilisateur, je veux que la carte BOB affiche un compteur `+N tâches` quand des cartes sont empilées derrière, afin de savoir combien tournent.
16. En tant qu'utilisateur, je veux distinguer visuellement la carte BOB (orchestrateur) des sous-tâches (teinte lavande, glyph ◇, mention « par BOB »), afin de ne pas confondre les deux niveaux.
17. En tant qu'utilisateur, lors d'une simple question sans délégation, je veux que la carte BOB s'affiche sans section « tâches en arrière-plan », afin que le fil reste épuré.

### Données générées (colonne droite)
18. En tant qu'utilisateur, je veux que les artefacts produits par Bob (courriels, documents) apparaissent en **cartes** dans un dock DONNÉES GÉNÉRÉES, afin de retrouver ce qu'il a généré.
19. En tant qu'utilisateur, je veux qu'une **nouvelle carte** s'anime à son arrivée (highlight `fresh`), afin de remarquer qu'un artefact vient d'être produit.
20. En tant qu'utilisateur, je veux que les cartes **persistent** pendant la session (pas de disparition automatique), afin de ne pas perdre un artefact actionnable.
21. En tant qu'utilisateur, je veux voir un compteur d'artefacts actifs, afin de jauger ce qui s'accumule.
22. En tant qu'utilisateur, je veux qu'une carte porte un **titre** (titre de tâche), un sous-titre et une **icône de type** (courriel/document/…), afin de l'identifier d'un coup d'œil.
23. En tant qu'utilisateur, je veux **cliquer** une carte pour ouvrir son contenu en plein écran, afin d'en consulter le détail.

### Overlay plein écran
24. En tant qu'utilisateur, je veux que l'overlay ouvre l'artefact dans un cadre HUD (coins d'angle, faisceau, en-tête mono `BOB · GÉNÉRÉ`, réf), afin de rester dans le langage visuel de la conscience.
25. En tant qu'utilisateur, je veux un **rendu typé** : un courriel s'affiche comme un courriel (expéditeur, sujet, corps, pièces jointes), un document comme un document (pages ou markdown), afin de lire le contenu naturellement.
26. En tant qu'utilisateur, je veux qu'un deliverable composite (plusieurs sections) s'affiche en **stack** dans l'overlay, afin de garder le regroupement cohérent voulu par Bob.
27. En tant qu'utilisateur, je veux des actions en pied d'overlay — **lire à voix haute**, **ouvrir**, **fermer** — afin d'agir sur l'artefact.
28. En tant qu'utilisateur, je veux fermer l'overlay via Échap, le bouton ✕, le bouton FERMER ou un clic hors carte, afin d'en sortir facilement.
29. En tant qu'utilisateur, je ne veux **pas** que l'overlay s'ouvre tout seul ; je veux décider quand l'ouvrir, afin de garder un HUD calme.

### Réglages LLM
30. En tant qu'utilisateur, je veux un bouton **RÉGLAGES** en haut à droite, afin d'accéder aux options sans encombrer l'écran.
31. En tant qu'utilisateur, je veux basculer entre **Claude CLI** et **LM Studio** dans un segmented control, afin de choisir mon moteur.
32. En tant qu'utilisateur sous Claude CLI, je veux voir l'état « connecté » et le modèle fixe, afin de savoir que le pont local fonctionne.
33. En tant qu'utilisateur sous LM Studio, je veux saisir l'URL du serveur (avec des presets) et voir s'il est joignable, afin de me connecter à mon serveur local.
34. En tant qu'utilisateur sous LM Studio, je veux voir la **liste des modèles** locaux (nom, params, quant, RAM) et en sélectionner un, afin de charger le modèle voulu.
35. En tant qu'utilisateur, je veux régler la **longueur de contexte** d'un modèle local, afin d'arbitrer mémoire / fenêtre.
36. En tant qu'utilisateur, je veux que mes choix de moteur/modèle **persistent** entre les lancements, afin de ne pas reconfigurer à chaque fois.

### Identité, saisie, voix
37. En tant qu'utilisateur, je veux voir `● BOB · {état}` en haut à gauche avec l'état courant, afin de toujours savoir où en est la conscience.
38. En tant qu'utilisateur, je veux une **tagline** de piste sous l'identité, afin d'ancrer l'ambiance.
39. En tant qu'utilisateur, je veux un **champ de saisie** minimal toujours visible en bas, afin de pouvoir poser une question à tout moment.
40. En tant qu'utilisateur, je veux une **ligne de transcription** (caption) en bas pendant la voix, afin de suivre ce qui est dit.
41. En tant qu'utilisateur, je veux un bouton **mute** en bas à droite, afin de couper la voix.
42. En tant qu'utilisateur, je veux pouvoir **déplacer la fenêtre** borderless via une zone de drag en haut, afin de la repositionner.

### États & démarrage
43. En tant qu'utilisateur, au **démarrage à froid** (aucune conversation), je veux voir l'orb + l'identité + l'input centrés, le deck et le dock estompés avec une invitation discrète, afin d'un accueil calme et non vide.
44. En tant qu'utilisateur, je veux que le deck et le dock **apparaissent en fade** dès la première donnée, afin de sentir l'app prendre vie.
45. En tant qu'utilisateur, je veux que les animations (glide du deck, jitter, fresh-in, respiration) suivent le **timing réel** des événements, afin d'une vivacité honnête sans latence artificielle.
46. En tant qu'utilisateur, je veux que Bob puisse parler de façon **proactive** (synthèse spontanée) et que sa carte s'affiche même sans prompt, afin de recevoir ses initiatives.

### Développeur / fidélité
47. En tant que développeur, je veux pouvoir lancer un mode `?dev` pour **tuner les paramètres de l'orb** (motion/glow/mood…), afin d'ajuster le rendu sans recompiler.
48. En tant que développeur, je veux que les modules de logique pure (état orb, deck, projection carte, narration) soient **testables en isolation**, afin de sécuriser les comportements clés.
49. En tant que développeur, je veux que la fenêtre `legacy` et la fenêtre `debug` restent **inchangées**, afin de conserver mes fallbacks.

## Implementation Decisions

### Stratégie & méthode
- **Refonte in-place** de la fenêtre HUD `new` uniquement. `legacy` (ChatView) et `debug` restent intactes. Aucune nouvelle fenêtre Tauri.
- **Réutilisation intégrale du backend / WS** : aucune nouvelle source de données. On câble les zones visuelles sur les événements existants (`speech_delta`, `thinking`, `task_created/updated/result`, `reasoning_delta`, `agent_activity`, `ui_payload`, `assistant_msg`, événement `perf`) et les endpoints `/api/llm/*`.
- **Port verbatim** de `p3d.css` (classes du mockup) dans une feuille de style **scopée**, et conversion des composants `p3d-*.jsx` en TSX câblés aux stores. Le mockup étant déjà du React, on privilégie la copie sur la réécriture.
- **Réconciliation CSS obligatoire** : `p3d.css` et la feuille HUD existante partagent des sélecteurs (`ov-*`, `overlay-*`, `panel*`, `md-*`). Le port ne doit **pas** être un ajout aveugle : les règles d'overlay du mockup **supersèdent** les règles d'overlay actuelles (même rôle), et les sélecteurs génériques (`panel*`) sont préfixés/scopés pour éviter les collisions avec le reste du HUD.

### Module : orb conscience (rendu)
- On porte l'orb **conscience** du mockup (shader WebGL + couche « life » + wrapper) et il **remplace** le rendu d'orb actuel. Palette **nacre** rose/lavande. Presets d'humeur `idle/listen/think/speak/alert/error`.
- Interface conceptuelle : un composant orb piloté par `{ state, energy, palette, tint, motion, glow }`. Pas de logique de dérivation d'état à l'intérieur (cf. module reducer).
- Le réglage des paramètres (motion/glow/mood/variant) reste exposé derrière `?dev` via le store de tweaks existant.

### Module : reducer d'état orb (pur, deep)
- `deriveOrbState(chatState, tasksState) → { state, energy }`. Étend la logique de dérivation d'état existante pour intégrer les tâches : tâches en cours ⇒ humeur « délégation/écoute », réponse en streaming ⇒ « parole », erreur/échec ⇒ « erreur/alerte », sinon « repos/réflexion ».
- L'`energy` (intensité) est dérivée de la phase de la carte au premier plan (réflexion/délégation/réponse), comme le mockup mappe phase → énergie.
- Module **sans dépendance UI**, testable isolément.

### Module : modèle de deck (pur, deep)
- `buildDeck(bobThread, subTasks, pinnedId) → orderedCards[]` avec, par carte : `kind` (bob|sub), `phase`, `frac`, `rank`, et les entrées de transform (rang → translate/scale/rotateZ jitter, opacité, z-index). Sélection de la carte de front (auto = la plus récemment active, ou la carte épinglée), promotion via pin avec maintien temporel.
- Ordre DOM stable (bob puis sous-tâches dans l'ordre déclaré) — seul le transform glisse — pour que les cartes restent les mêmes éléments entre les reshuffles (reprend la logique `useThread`/`ThreadStack` du mockup).
- Module **pur**, testable isolément.

### Module : projection deliverable → carte (pur, deep)
- `toCard(deliverable, task) → { title, sub, type, sections[] }`. Décision **1 carte par deliverable** : un `result_payload` (liste de `ComponentDescriptor`) ou un `ui_payload` de Bob produit **une** carte de dock.
- `title` = titre de la tâche (`Task.title`) ; `sub` = goal/résumé court ; `type` = type dominant des sections (icône), avec un glyph **composite** si les sections sont hétérogènes.
- `sections[]` = les `ComponentDescriptor` tels quels, consommés par l'overlay (stack).
- Module **pur**, testable isolément.

### Module : narrated fallback (pur, deep)
- `narrate(events) → reflectionLine`. Quand le thread principal ne fournit pas de `reasoning_delta` (modèle non reasoning-capable), produit une ligne de réflexion **dérivée des événements** (mêmes principes que la projection d'activité existante). Quand `reasoning_delta` est présent, c'est lui qui prime.
- Module **pur**, testable isolément.

### UI portée (composants TSX)
- **Scène / shell** : fond + grain, identité haut-gauche (`● BOB · {état}` + tagline statique « nacre — sphère liquide · sanctuaire en profondeur »), stage 3D (emplacements core / fil / données), orchestration du **fade-in** au repos.
- **Deck gauche** : `ThreadDeck` rend les cartes depuis le modèle de deck ; `BobCard` (prompt / réflexion / tâches en arrière-plan / réponse / perf) et `SubCard` (réflexion / outil / rendu). **Remplace** le panneau d'activité agent actuel. Sources : store d'activité (reasoning/activity), événements de tâches, store de chat.
- **Dock droite** : `DataDock` + `DataCard` rendent les deliverables persistés depuis le store de deliverables.
- **Overlay** : l'overlay de sections actuel est **re-skinné** au chrome du mockup (coins, faisceau, en-tête mono + réf, surfaces typées, pied d'actions). Conserve le **stack** de sections (composite). Surfaces portées : **Mail** et **Document** uniquement. Le rendu markdown réutilise le rendu markdown existant (plus robuste que le mini-parser du mockup), stylé aux classes du mockup.
- **Réglages** : `SettingsModal` (gear haut-droite) **remplace** le picker actuel (haut-gauche). Segmented Claude CLI ↔ LM Studio, URL + presets, liste de modèles (params/quant/RAM), slider de longueur de contexte. Câblé aux endpoints `/api/llm/*` existants (`GET selection`, `GET models`, `PUT selection`). Persistance des choix via le mécanisme déjà en place.

### Store
- **Store de deliverables** (nouveau) : collection scope-session des deliverables générés (issus de `task_result.result_payload` et des `ui_payload` de Bob), avec état `fresh` (vient d'arriver) / `seen`. Alimente le dock et l'overlay. Pas de TTL : pas d'éviction automatique ; les cartes restent jusqu'à fin de session ou dismiss manuel.

### Ajustements backend (minimaux)
- **`agent_ref` du thread principal** : garantir que l'orchestrateur Bob émet ses `reasoning_delta` et ses métriques `perf` avec un `agent_ref` stable, afin que la carte BOB s'y bind comme les sous-tâches se bindent au leur. À confirmer puis ajuster si absent.
- **Perf turn-level** : les `tok_s` / `ttft` / `tokens_in` sont produits **par appel LLM**. Le footer de la carte BOB affiche une vue **agrégée sur le tour** — agrégation faite côté front à partir des événements `perf` si possible, sinon petit ajout backend.

### Interactions clés
- **Atterrissage des deliverables** : `task_result` (et `ui_payload` de Bob) → projection → carte ajoutée au store (`fresh`) → apparaît animée dans le dock. **Pas d'auto-ouverture** d'overlay. La réponse synthétisée reste lisible dans la carte BOB.
- **Ouverture overlay** : clic sur une carte du dock → overlay stack de ses sections → `fresh` passe à `seen`.
- **Promotion deck** : clic sur une carte arrière → pin temporel → elle passe au front ; sinon le front est auto-sélectionné par activité.
- **État orb** : reducer alimenté en continu par chat + tâches → pilote l'humeur/énergie de l'orb conscience.

## Testing Decisions

Un bon test ici vérifie le **comportement externe** d'un module, pas son implémentation : on
donne des entrées (état chat/tâches, liste d'événements, deliverable) et on asserte la sortie
(état orb, ordre/rangs des cartes, descripteur de carte, ligne de réflexion). Aucun test ne doit
inspecter de détail interne ni dépendre du rendu WebGL/CSS ou des timings d'animation.

Modules **testés** (logique pure, deep modules) :
- **Reducer d'état orb** — cas : repos, réflexion, délégation (tâches en cours), réponse en streaming, erreur ; vérifier `state` + `energy`. Prior art : tests de dérivation d'état orb et de phase d'agent existants.
- **Modèle de deck** — cas : Bob seul (pas de sous-tâches), Bob + N sous-tâches, sélection auto du front, promotion par pin, stabilité de l'ordre DOM ; vérifier rangs, front, transforms attendus. Prior art : tests utilitaires purs existants (regroupement d'événements, phase d'agent).
- **Projection deliverable → carte** — cas : deliverable mono-type (mail), multi-mail, composite hétérogène, `ui_payload` de Bob ; vérifier `title` / `sub` / `type` (dominant vs composite) / `sections`. Prior art : test d'heuristique d'overlay existant.
- **Narrated fallback** — cas : présence de `reasoning_delta` (prime), absence (narration dérivée des événements), événements partiels ; vérifier la ligne produite. Prior art : test de phase d'agent.

Modules **non testés** (hors scope tests) : composants UI portés (orb, scène, deck, dock,
overlay, settings) et CSS — validés à l'œil / en run. Le store de deliverables peut faire l'objet
de tests d'intégration légers si besoin (ajout / `fresh`→`seen` / pas d'éviction), mais n'est pas
prioritaire.

## Out of Scope

- **Nouvelles fenêtres** Tauri : on ne refond que la fenêtre `new`.
- **Types de données video / contact / action** : on ne porte (pour l'instant) que les surfaces **Mail** et **Document**. Les autres surfaces et leurs connecteurs (calendar, drive, caméra) et le flux « action à valider » sont différés.
- **Connecteurs** au-delà de Gmail : pas de nouveau connecteur dans cette refonte.
- **Switcher de pistes / thèmes** user-facing (Iris / Aurore / Céladon, variantes pearl/particles/aurora/rings) : on n'embarque que **Nacre + nebula**. Le tuning d'orb reste réservé à `?dev`.
- **Mode démo / scénario scripté** : le scénario du mockup (Daniela / budget v4 / …) est **jeté** — données 100% live. Pas d'« attract mode ».
- **TTL / éviction automatique** des cartes : remplacée par une persistance scope-session.
- **ChatView legacy** et **DebugView** : inchangées.

## Further Notes

- **Écarts assumés vs fidélité pure** (choix produit explicites lors du grilling) : persistance des
  cartes au lieu du TTL ~11s ; 2 types réels au lieu de 5 ; 1 carte par deliverable + overlay stack
  au lieu d'1 carte par artefact ; live only sans démo. Le reste vise la fidélité maximale.
- **Risques d'implémentation à surveiller** :
  1. Collision de sélecteurs CSS entre le port `p3d.css` et la feuille HUD existante (`ov-*`,
     `overlay-*`, `panel*`, `md-*`) — réconcilier, ne pas ajouter en aveugle.
  2. Le port de l'orb conscience (WebGL : shader + couche « life ») est le morceau le plus
     conséquent et le plus central — à dérisquer tôt.
  3. Confirmer l'`agent_ref` du thread principal Bob pour router reasoning/perf vers la carte BOB.
  4. Les args/résultats des `tool_call` peuvent être **redacted** côté projection d'activité —
     prévoir un fallback `nom d'outil + état` sur la SubCard.
  5. Agrégation des métriques perf (par appel LLM → vue par tour).
- **Source de vérité design** : `Design Mockup/Bob - Pistes 3D.html` et les fichiers `p3d-*.jsx` /
  `p3d.css` ; screenshots de référence `p3d-default`, `p3d-settings`, `01-piste`, `p3d-all`.
- **Cohérence index features** : cette refonte est la 14ᵉ feature (suite logique de
  `docs/features/0001`→`0013`) ; le PRD est numéroté `0014`. Penser à indexer la feature livrée
  dans `CLAUDE.md`.
- **Suite recommandée** : générer les issues tracer-bullet (slices verticaux) à partir de ce PRD,
  en dérisquant d'abord l'orb + la réconciliation CSS, puis le deck gauche, le dock droite,
  l'overlay et les réglages.
