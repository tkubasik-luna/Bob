# PRD 0010 — Adaptive Composite UI (sections list overlay)

## Problem Statement

Quand l'utilisateur demande plusieurs éléments à la fois — « donne-moi mes 3 derniers mails » — Bob n'en affiche qu'un seul. La recherche Gmail récupère bien les 3 messages (le résumé vocal dit « 3 email(s) trouvé(s) »), mais l'overlay visuel ne montre que le premier. L'utilisateur voit une réponse parlée qui contredit ce qui s'affiche : 1 carte là où il en attend 3.

Plus largement, chaque surface visuelle de Bob (un mail, un bloc Markdown) est aujourd'hui un overlay « mono-composant » : le deliverable d'une sous-tâche est UN seul `{component, props}`, et le HUD ne sait afficher qu'une carte unique à la fois. Bob ne peut donc pas composer une vue à partir de plusieurs morceaux (« voici un résumé, puis ces 3 mails »), ni faire évoluer ce qu'il montre sans qu'un développeur câble un nouvel overlay à la main.

## Solution

Du point de vue de l'utilisateur : Bob affiche désormais des **vues à sections empilées**. Une réponse visuelle est une liste ordonnée de sections (un mail, un autre mail, un bloc Markdown…), rendue dans un seul overlay HUD à cadre unique avec défilement. « Mes 3 derniers mails » produit 3 cartes Mail empilées, chacune avec ses propres actions (OPEN, READ ALOUD). Une réponse à un seul élément reste une vue à une seule section — rien ne régresse visuellement.

Bob compose la vue lui-même, à partir d'un **catalogue fixe de sections disponibles** : il choisit quelles sections afficher et dans quel ordre, mais ne fabrique jamais de HTML ni les données d'une carte. Les données (props d'un mail) sont produites de façon déterministe côté serveur ; le LLM ne fait que désigner quel résultat surfacer. Si Bob référence une section que le frontend ne sait pas encore rendre, l'utilisateur voit une carte « Section non supportée » lisible plutôt qu'un écran vide ou un crash. Si une section est malformée, les autres s'affichent quand même.

## User Stories

1. En tant qu'utilisateur, je veux que « donne-moi mes 3 derniers mails » affiche 3 cartes mail, afin que ce que je vois corresponde à ce que Bob me dit.
2. En tant qu'utilisateur, je veux demander un nombre quelconque de mails (jusqu'à la limite outil) et les voir tous empilés, afin de parcourir ma boîte sans rouvrir l'app Gmail.
3. En tant qu'utilisateur, je veux que chaque carte mail garde ses actions (OPEN, READ ALOUD), afin d'agir sur n'importe quel mail de la liste, pas seulement le premier.
4. En tant qu'utilisateur, je veux pouvoir faire défiler une vue qui contient plus de sections que la hauteur d'écran, afin de tout consulter.
5. En tant qu'utilisateur, je veux fermer toute la vue d'un seul geste (Esc / DISMISS / clic backdrop), afin de revenir à la sphère rapidement.
6. En tant qu'utilisateur, je veux qu'une réponse à un seul mail s'affiche exactement comme avant, afin de ne subir aucune régression sur les cas simples.
7. En tant qu'utilisateur, je veux qu'une réponse purement vocale courte n'ouvre pas de carte parasite, afin que l'overlay ne s'impose que quand il y a du contenu à voir.
8. En tant qu'utilisateur, je veux qu'une réponse contenant une section riche (un mail) ouvre l'overlay automatiquement, afin de ne pas avoir à le déclencher manuellement.
9. En tant qu'utilisateur, je veux qu'une section que Bob référence mais que l'app ne sait pas rendre s'affiche comme « Section non supportée : X », afin de comprendre ce qui manque sans bug visuel.
10. En tant qu'utilisateur, je veux qu'une section malformée n'efface pas les sections valides, afin de toujours voir le maximum de contenu exploitable.
11. En tant qu'utilisateur, je veux que Bob compose l'ordre des sections de façon pertinente, afin que la vue soit lisible (résumé d'abord, détails ensuite).
12. En tant que développeur, je veux un seul chemin de rendu d'overlay (une liste de sections), afin de ne plus maintenir des overlays mono-composant divergents.
13. En tant que développeur, je veux ajouter un nouveau type de section en l'enregistrant dans le catalogue, afin d'étendre l'UI sans recâbler le dispatch.
14. En tant que développeur, je veux que les props des sections de données soient produites par du code déterministe, afin de ne pas dépendre d'un modèle local faible pour la justesse des données.
15. En tant que développeur, je veux que le deliverable d'une sous-tâche et le `say.ui` de Jarvis partagent exactement la même forme (liste de sections), afin que les deux surfaces ne puissent pas diverger.
16. En tant que développeur, je veux que la validation drop les sections invalides plutôt que rejeter tout le payload, afin qu'une erreur partielle n'annule pas la réponse entière.
17. En tant que développeur, je veux que la lecture d'un ancien `result_payload` (objet single) ne crashe jamais, afin que les lignes pré-migration restent inoffensives.
18. En tant que développeur, je veux que référencer un résultat d'outil (`result_ref`) suffise à surfacer toutes ses sections, afin que le LLM n'ait pas à énumérer les éléments un par un.
19. En tant que mainteneur, je veux supprimer les overlays mono-composant obsolètes une fois la vue à sections en place, afin de réduire la dette de surfaces redondantes.
20. En tant qu'utilisateur sur modèle local faible, je veux que la vue multi-éléments fonctionne même quand le modèle ne sait pas fabriquer de props, afin que la robustesse ne dépende pas de la qualité du LLM.

## Implementation Decisions

### Contrat LLM → UI (conteneur)

- Le payload visuel canonique est une **liste nue** de descripteurs : `ComponentDescriptor[]`. Pas de composant `View`/`Sections` wrapper, pas de clé `sections`.
- On réutilise le schéma `ui` déjà existant (le `oneOf` par composant et le tableau de la réponse `{speech, ui}`). Aucun nouveau schéma : la même définition valide à la fois `say.ui` et le deliverable de sous-tâche.
- `say.ui` (Jarvis) est déjà un tableau. Le deliverable de sous-tâche (`result_payload`) passe d'un objet single à un tableau, convergeant sur la même forme.

### Composition (hybride)

- **Le projector déterministe produit les sections de données.** `project_gmail_search` émet une section `Mail` par message retourné, dans l'ordre du résultat, au lieu de `messages[0]` uniquement.
- **Le LLM choisit quel résultat surfacer**, via un unique `result_ref` dans son action `done`. Un `result_ref` s'expanse en la **liste complète** des sections projetées de ce résultat. Le LLM n'énumère pas les éléments et ne rédige jamais les props d'une carte de données.
- Un deliverable rédigé par le LLM (rapport Markdown via `ui_payload`) reste possible et devient une liste à une seule section Markdown.

### Backend — modules

- **Section list validator** (dans le registre UI) : valide une liste de descripteurs section par section. Interface : retourne les sections conservées + la liste des erreurs des sections droppées. Une section à composant inconnu ou props invalides est exclue ; les sections valides passent. Remplace le comportement « tout ou rien » au niveau liste pour le chemin deliverable.
- **Deliverable resolver** (runner) : `_resolve_terminal_deliverable` retourne `list[ComponentDescriptor] | None` (liste vide normalisée en `None`). Quand un `result_ref` est fourni, il résout la liste de sections du projector correspondant.
- **Projected result** (result store) : le champ `deliverable` d'un résultat projeté devient `list[ComponentDescriptor] | None`. `default_projector` continue de ne rien produire (`None`).
- **Gmail projector** (tool registry) : émet `[Mail(props) for props in messages]`. Le digest transcript et le résumé vocal déterministe sont inchangés (le résumé reste « N email(s) trouvé(s)… »).
- **result_payload codec** (task store) : la colonne reste TEXT JSON. Le décodage produit une `list`. **Décodage défensif obligatoire** : toute valeur non-liste (ancien objet single, JSON corrompu, `null`) est ramenée à une liste vide — jamais d'exception. C'est un invariant, pas un back-fill : les anciennes lignes ne sont pas migrées, juste rendues inoffensives.
- **WS `task_result`** : transporte le `result_payload` liste tel quel.

### Frontend — modules

- **Section registry** : table `nom de composant → { Component, structured: bool }`. MVP : `Mail` (structured = true), `Markdown` (structured = false). Le flag `structured` pilote l'auto-open (story 7/8).
- **NotImplemented** : composant de repli rendu quand le nom de section est absent du registry. Affiche « Section non supportée : <ComponentName> » + courte hint. Aucun rendu des props brutes. Reprend l'esprit du bloc ambre `UnknownComponent` existant.
- **MailCard** : extrait du corps de l'ancien `MailOverlay` — avatar, expéditeur, sujet, snippet, flags, attachments, et **actions inline** (OPEN vers `gmailWebUrl`, READ ALOUD). Sans chrome d'overlay (pas de corner-brackets ni header/footer propres).
- **MarkdownSection** : corps Markdown extrait de l'ancien `MarkdownOverlay`, sans chrome.
- **SectionsOverlay** : le seul shell d'overlay. Possède le cadre (corner-brackets), le header, le DISMISS global, le backdrop, l'écoute Esc, et un **stack scrollable** de sections. Mappe chaque descripteur via le section registry, repli sur `NotImplemented` si inconnu.
- **Dispatch** (SphereUI) : état unique `overlaySections: ComponentDescriptor[] | null`. `openOverlayFromDescriptor` devient `openOverlayFromSections(sections)`. **Auto-open** : ouvre si la liste contient ≥1 section `structured`; sinon (Markdown/texte seul) applique l'heuristique `shouldOverlayResponse` actuelle sur le texte. La dédup par source (msg id, task id) et le path streaming sont conservés.
- **Big-bang** : `MailOverlay` et `MarkdownOverlay` (versions mono-composant) sont supprimés une fois `SectionsOverlay` en place. `TaskOverlay` (overlay par-tâche au clic) reste, mais consomme la même liste de sections pour son rendu de résultat.

### Catalogue MVP

- `Mail`, `Markdown`, `NotImplemented`. `ChatMessage` reste réservé au chat-path (non surfacé en overlay). Aucun composant spéculatif (List/KeyValue/…) introduit ici.

### Invariants de robustesse

- Une section invalide ne blanchit jamais la vue (drop par-section).
- Une section inconnue ne crashe jamais le rendu (repli NotImplemented).
- Un `result_payload` non-liste ne lève jamais d'exception (décodage défensif).
- Les props des sections de données restent déterministes (jamais rédigées par le modèle faible).

## Testing Decisions

Décision : **tests pour tous les modules**. Un bon test vérifie le comportement externe observable (l'interface du module), pas les détails d'implémentation : on teste « ce qui sort pour ce qui entre », pas la structure interne. On évite de coupler les tests aux noms privés ou à l'ordre des appels.

Backend (pytest, prior art : `test_gmail_projector.py`, `test_gmail_search_branches.py`, `test_event_bus_v2.py`, tests du result store) :

- **Gmail projector** : un résultat à N messages produit N sections `Mail` dans l'ordre ; un résultat à 0 message produit `None` (ou liste vide normalisée) ; le digest et le résumé vocal sont inchangés ; chaque section porte des props `Mail` valides.
- **Section list validator** : une liste toute-valide passe intacte ; une section à props invalides est droppée et les valides conservées ; une section à composant inconnu est droppée (avec erreur reportée) ; une liste vide est gérée ; les erreurs reportées sont exploitables pour la self-correction.
- **Deliverable resolver** : un `result_ref` vers un résultat multi-sections résout la liste complète ; absence de deliverable → `None` ; un `ui_payload` Markdown rédigé devient une liste à une section.
- **result_payload codec** : décodage d'un tableau JSON → liste ; décodage d'un ancien objet single → liste vide (pas de crash) ; décodage de `null` / JSON corrompu → liste vide ; round-trip encode/décode d'une liste.

Frontend (prior art : `MailOverlay.test.tsx`, `MarkdownOverlay.test.tsx`, `SphereUI.test.tsx`, tests de `Dispatcher`) :

- **Section registry / NotImplemented** : un composant connu rend son renderer ; un composant inconnu rend `NotImplemented` avec le nom affiché ; aucun rendu de props brutes.
- **SectionsOverlay** : une liste de 3 `Mail` rend 3 cartes ; mélange Mail + Markdown rend dans l'ordre ; section inconnue → carte NotImplemented intercalée sans casser les voisines ; DISMISS / Esc / backdrop ferment ; scroll présent au-delà du seuil.
- **MailCard** : actions inline OPEN (route vers `gmailWebUrl` via le seam de test) et READ ALOUD présentes par carte ; rendu correct des flags / attachments.
- **Dispatch auto-open (SphereUI)** : liste contenant une section structurée (Mail) → overlay ouvert ; liste Markdown-seul court → overlay fermé (heuristique) ; dédup par source ne réouvre pas après dismiss ; `result_payload` liste d'une tâche terminée ouvre la vue.

Critères d'acceptation de bout en bout :

- « donne-moi mes 3 derniers mails » → 3 cartes Mail empilées dans `SectionsOverlay`.
- Section inconnue → carte NotImplemented, pas de crash.
- 1 section invalide dans la liste → les autres s'affichent.
- Réponse à 1 seul mail → vue à 1 section, équivalente à l'ancien overlay.

## Out of Scope

- Composant `View`/`Sections` wrapper explicite (rejeté au profit de la liste nue).
- Composition multi-`result_ref` ordonnée ou mélange refs + sections littérales rédigées par le LLM (le LLM ne surface qu'un `result_ref` qui s'expanse).
- Nouveaux composants riches (List, KeyValue, Card, Map…) — l'architecture les permet mais aucun n'est livré ici.
- Surfacer `ChatMessage` en overlay.
- Back-fill / migration des anciennes lignes `result_payload` (gérées par décodage défensif, pas réécrites).
- Barre d'action globale au niveau overlay / notion de section focus/sélectionnée (rejeté : actions inline par section).
- Rollout incrémental derrière flag (rejeté : big-bang assumé).
- Streaming incrémental section-par-section de l'overlay (la vue s'ouvre sur une liste complète ; le path streaming `ui_payload` existant est conservé tel quel, non étendu au rendu progressif multi-sections).

## Further Notes

- Déclencheur précis confirmé dans les logs : tâche `29de9af8`, 2026-05-30 13:58 — `gmail_search` retourne `count=3`, mais le deliverable projeté ne contient que `messages[0]`.
- S'appuie sur PRD 0009 (Tool Result Store) : projector déterministe, `ProjectedResult`, convergence sur résultat terminal. La leçon centrale de 0009 — retirer la fabrication de props au modèle local faible — est préservée : seul le code produit les props de données.
- S'appuie sur PRD 0008 (Gmail Mail Overlay) : `MailOverlay`, props `Mail`, `result_payload` JSON. Ce PRD en généralise le rendu vers une liste de sections et démantèle l'overlay mono-composant.
- Modèle cible local faible : `qwen/qwen3.5-9b`. Robustesse = barre n°1 ; tous les chemins doivent dégrader proprement (drop, repli, décodage défensif) plutôt qu'échouer.
- Convergence de forme : à l'issue, `say.ui` et `result_payload` parlent strictement `ComponentDescriptor[]`, et `openOverlayFromSections` est l'unique point d'entrée du rendu d'overlay.
