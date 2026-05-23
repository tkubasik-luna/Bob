# 0004 — Sphere HUD UI (Jarvis-style visual shell)

## Problem Statement

L'UI actuelle de Bob est un chat utilitaire : bulle user / bulle assistant qui scroll, sidebar de sous-tâches, drawer pour transcript. C'est fonctionnel mais sans présence : Bob ressemble à un chatbot web, pas à un assistant personnel ambiant. L'utilisateur veut une expérience visuelle cinématique type "Jarvis" — une sphère centrale vivante qui respire, pense, parle, avec un HUD chrome minimaliste autour. Une mockup complète (HTML/React + shader WebGL2 + screenshots) existe déjà dans `Design Mockup/` à la racine du repo et fait référence (`THEME='warm'`, `MOOD='calm'`, `VARIANT=0 liquid` locked).

Sans cette refonte, Bob reste perçu comme une page de chat de plus, là où l'intention produit est "compagnon assistant qui parle". L'input vocal n'est pas encore disponible (le hook `useVoiceMode` actuel ne fait que toggle TTS), donc le nouveau shell doit aussi prévoir un text input field bas en attendant le support speech-to-text.

## Solution

Refonte de la couche UI (frontend uniquement, WS protocol inchangé) sur la base du mockup `Design Mockup/`. Le shell est composé de :

- **Sphère WebGL2 centrale** : full-screen canvas avec fragment shader (6 variantes × 6 états) porté tel quel du mockup. V1 lock `warm + calm + liquid` (dev mode permet de switch). La sphère **respire** en `idle`, **pense** avec un swirl turbulent + thoughts drifting en `think`, **pulse audio-réactive** en `speak`, **glitch** en `error`.
- **HUD chrome minimal V1** : tasks panel top-right (binding direct sur le store Zustand `tasks`), transcript line bas (1 ligne, fade in/out), input text fixe sous la transcript line, mute toggle bottom-right + raccourci `M`. Le reste du HUD (identity, telemetry, state rail, signal ticks, diag, frame corners, surface picker, 6 autres overlays surface) est **hors scope V1**.
- **Overlay markdown** : quand Jarvis répond avec du contenu structuré (markdown détecté OU > 3 lignes), un overlay card style "Notes" du mockup s'ouvre au centre par-dessus la sphère. Cross-fade in-place sur réponse suivante. Ferme via `Esc` / bouton X / clic dehors.
- **État sphère dérivé client** : `idle` quand WS OK rien ne se passe, `think` quand `isWaitingResponse=true`, `speak` quand `speakingMsgId` actif (TTS en cours) **ou** stream de texte assistant en cours, `error` quand WS disconnected ou backend error. Zéro changement backend.
- **Audio reactivity réelle** : la sphère pulse avec le RMS de l'audio TTS sortant (Web Audio `AnalyserNode` tap dans `audioPlayer.ts`).
- **Dev mode** : URL query `?ui=new&dev=1` révèle state pills (1-6), tweaks panel (motion/glow/variant/mood/theme/autoCycle), keyboard shortcuts mockup (1-6 force state). Production = clean.
- **2 fenêtres Tauri en dev** : la fenêtre `legacy` (`?ui=legacy`) montre toujours `ChatView` actuel, la fenêtre `new` (`?ui=new`) montre la nouvelle Sphere UI. Permet comparaison visuelle pendant l'implémentation. Quand la nouvelle UI est stable, on flippe le défaut.
- **Tauri borderless** : `decorations: false` + drag region custom (~28px top transparent). Les frame corners du mockup deviennent le chrome visuel.

L'utilisateur garde son chat existant (legacy) intact tout au long du dev. Une fois la sphere UI stable, on supprime `ChatView`, `ChatMessageBlock`, `TaskCard`, `TaskSidebar`, `TaskDrawer`, `Dispatcher`, `registry` (deviennent obsolètes — pas de chat scroll, pas de sidebar/drawer). On garde `MarkdownView` (réutilisée dans overlay), `Toast` (restylé HUD), `useWebSocket`, `useVoiceMode`, `audioPlayer`, le `chatStore`.

## User Stories

1. As an utilisateur, I want voir une sphère vivante au centre de l'écran qui respire calmement quand Bob attend, so that je sente la présence du compagnon plutôt qu'une page de chat statique.

2. As an utilisateur, I want que la sphère bascule visuellement quand Bob réfléchit (`think`) avec un swirl turbulent et des fragments de pensée qui dérivent, so that je sache qu'il bosse sans avoir à lire un texte "thinking…".

3. As an utilisateur, I want que la sphère pulse en rythme avec la voix de Bob quand il parle (`speak`), so que je voie qu'il est en train de me répondre même si j'ai détourné le regard.

4. As an utilisateur, I want que la sphère affiche un glitch chromatique / scanline noise quand le backend WS tombe (`error`), so que je perçoive immédiatement qu'il y a un problème sans devoir lire un toast.

5. As an utilisateur, I want taper un message à Bob dans un champ texte fixe en bas de l'écran, so que je puisse interagir tant que la reconnaissance vocale n'est pas implémentée.

6. As an utilisateur, I want voir mon dernier message + la dernière réponse courte de Bob en une ligne de transcript juste au-dessus du champ texte, so que je garde le fil sans avoir besoin d'un chat scrollable.

7. As an utilisateur, I want que les réponses longues ou structurées (markdown avec titres, listes, code, tables) s'affichent dans un overlay card cinématique par-dessus la sphère, so que je puisse lire confortablement sans casser l'expérience visuelle.

8. As an utilisateur, I want fermer l'overlay markdown avec `Esc`, le bouton `×`, ou un clic en dehors de la card, so que je contrôle quand je reviens à la vue sphère pure.

9. As an utilisateur, I want que les courtes réponses (1-2 lignes plain text type "il est 14:32") restent dans la transcript line sans ouvrir d'overlay, so que l'overlay ne pollue pas chaque tour de parole.

10. As an utilisateur, I want voir un petit panel "tâches en cours" dans le coin supérieur droit qui liste mes Jarvis sub-tasks (queued / running / done / failed), so que je sache ce que Bob bricole en arrière-plan.

11. As an utilisateur, I want que les tâches du panel affichent leur état avec une icône claire (ring vide queued, arc qui tourne running, check done, croix error), so que je comprenne en un regard où elles en sont.

12. As an utilisateur, I want pouvoir muter la voix de Bob via un petit bouton bottom-right ou la touche `M`, so que je puisse continuer à utiliser Bob en réunion sans qu'il parle à voix haute.

13. As an utilisateur, I want que la fenêtre Bob n'ait plus de barre de titre OS classique (Tauri borderless), so que le mockup cinématique tienne sa promesse esthétique sans cadre intrusif.

14. As an utilisateur, I want pouvoir déplacer la fenêtre via une drag region en haut (les ~28px transparents), so que je puisse repositionner Bob malgré l'absence de title bar.

15. As an utilisateur, I want que la sphère / HUD respectent un look unique "warm calm liquid" en V1 (palette orange chaude + désaturation calme + variant liquid mercury), so que l'aesthetic soit cohérent avec le mockup validé.

16. As an utilisateur, I want que la nouvelle UI se lance par défaut quand Bob démarre, so que je n'aie rien à configurer une fois la feature stable.

17. As un développeur, I want pouvoir lancer `./scripts/dev.sh` et voir s'ouvrir 2 fenêtres Tauri (legacy + new) côte à côte, so que je puisse comparer pixel par pixel le comportement actuel et la cible pendant l'implémentation.

18. As un développeur, I want que la fenêtre legacy expose toujours `ChatView` actuel inchangé pendant tout le dev, so que je puisse continuer à utiliser Bob pour des tâches réelles tant que la nouvelle UI n'est pas prête.

19. As un développeur, I want que les deux fenêtres Tauri partagent le même backend / WS (même chatStore), so que les messages et tâches restent cohérents entre les deux vues.

20. As un développeur, I want activer un dev mode via `?ui=new&dev=1` qui révèle les state pills (1-6), un tweaks panel (motion / glow / variant / mood / theme / autoCycle), so que je puisse tester visuellement chaque état/variant/mood sans devoir simuler des events backend.

21. As un développeur, I want que la sphère / HUD survivent sans WebGL2 avec un message d'erreur lisible (pas un écran noir), so que je sache exactement pourquoi ça ne marche pas si je suis sur une machine bizarre.

22. As un développeur, I want que la dérivation d'état sphère soit une fonction pure (`useSphereState` hook) entièrement testable en isolation, so que je puisse vérifier tous les couples `(WS state, isWaitingResponse, speakingMsgId, voiceEnabled) → sphereState` sans monter le DOM.

23. As un développeur, I want que l'heuristique "ouvrir l'overlay markdown ou pas" soit une fonction pure (`shouldOverlayResponse(content)`) testable en isolation, so que je puisse couvrir tous les cas (1 ligne plain, 3 lignes plain, list, table, code fence, mix) sans monter de composant.

24. As un développeur, I want que le tap audio (`useAudioLevel`) ait un fallback propre à 0 si `AudioContext` n'est pas dispo, so que la sphère ne crashe pas tant qu'aucune réponse TTS n'a démarré.

25. As un développeur, I want que les anciens composants (`ChatView`, `ChatMessageBlock`, `TaskCard`, `TaskSidebar`, `TaskDrawer`, `Dispatcher`, `registry`) soient supprimés du repo dès que la nouvelle UI prend le défaut, so que le codebase ne traîne pas de dead code.

26. As un développeur, I want que `MarkdownView` reste utilisé tel quel (port + restyle CSS uniquement) à l'intérieur de l'overlay, so que je profite de `react-markdown` + `remark-gfm` (tables, GFM) sans réinventer un parser.

27. As un développeur, I want que les tokens design (`--bg`, `--accent`, `--hud-rule`, fonts) soient exposés à la fois en CSS vars classiques **et** via le bloc `@theme` Tailwind v4, so que je puisse mélanger CSS hand-written (pour HUD complexe / animations) et Tailwind utilities (pour composants simples) sans dériver.

28. As un développeur, I want que le HUD chrome (frame corners, zone layout, animations) vive dans un seul stylesheet global `hud.css` importé en haut de `main.tsx`, so que la fidélité au mockup soit triviale à maintenir (copie-colle).

29. As un développeur, I want que le shader WebGL2 (`sphere-shader.js`) soit porté tel quel depuis le mockup (480 lignes, 6 variants × 6 états compilés), so que je conserve tous les états même si V1 en lock un seul.

30. As un développeur, I want que le projet ait un test runner frontend (Vitest + `@testing-library/react` + `jsdom`) installé avec un script `pnpm test`, so que je puisse exécuter tous les tests unitaires des modules deep en CI ou en local.

## Implementation Decisions

### Modules nouveaux frontend

- **`SphereCanvas`** (deep) — Wrapper React du renderer WebGL2. Encapsule la création du contexte GL, la compilation shader, la boucle `requestAnimationFrame`, la crossfade des state weights, l'interpolation de couleur, et le tap glyph-overlay (variant 5). Interface : props `state`, `variant`, `motion`, `glow`, `theme`, `mood`, `audioLevel`. Aucune logique métier — purement render.
- **`useSphereState`** (deep) — Hook pure. Lit du store : `connectionStatus`, `isWaitingResponse`, `speakingMsgId`, et le booléen "stream texte assistant en cours" (à dériver de `messages[last].role === 'assistant'` + flag streaming). Retourne `'idle' | 'think' | 'speak' | 'error'`. Aucun side-effect.
- **`useAudioLevel`** (deep) — Hook qui installe un `AnalyserNode` sur l'`AudioContext` exposé par `audioPlayer.ts`, échantillonne le RMS en `requestAnimationFrame`, retourne un `useRef<number>` (0..1) ou un state si la consommation downstream l'exige. Fallback 0 si `audioPlayer` pas initialisé.
- **`shouldOverlayResponse(content: string): boolean`** (deep) — Fonction pure. Retourne `true` si le contenu matche une regex de structure markdown (`#`, `##`, `###`, liste `-`/`*`/`1.`, code fence ` ``` `, table `|`, blockquote `>`, lien `[..](..)`) **OU** si `content.split('\n').length > 3`. Sinon `false`.
- **`MarkdownOverlay`** — Composant overlay card. Wrapping React-markdown + remark-gfm avec un `components` mapping qui assigne les classes CSS portées du mockup (`md-h1`, `md-h2`, `md-p`, `md-quote`, `md-ul`, `md-ol`, `md-pre`, `md-hr`, `md-inline-code`, `md-link`). Gère `Esc` global keydown, clic outside via overlay backdrop, bouton `×` dans le header card. Header reprend le pattern mockup (`BOB · SURFACING` / `MARKDOWN` / `REF · NTS-XXXX` / close button).
- **`HudTasks`** — Panel top-right. Lit `useChatStore(s => s.tasks)`, map vers le format mockup (`queued | running | done | error` + `progress 0..1` + `name`). Affiche les 4 dernières (mockup `tasks.slice(-4)`). Hover ne crée pas de drawer (drawer killed). Animations `tasks-in` + `hud-task-in` portées tel quel.
- **`TranscriptLine`** — Composant zone bas centrée. Affiche en fade in/out : soit le dernier prompt user (pendant `think`), soit le snippet du dernier assistant message (pendant / après `speak`), soit le hint `"Tapez pour parler à Bob"` (en `idle` initial). Pas affiché quand `MarkdownOverlay` est ouvert (l'overlay prend le relais visuel).
- **`InputField`** — Text input fixe juste sous `TranscriptLine`. Style HUD (border `--hud-rule`, font `Space Grotesk`, placeholder discret). Enter envoie via WS (réutilise la même fonction que `ChatView`). Disabled visuellement pendant `think`/`speak` mais reste utilisable (envoyer un follow-up reste possible).
- **`MuteToggle`** — Petit bouton glyph en `bottom-right` (offset des frame corners). Icon haut-parleur normal vs haut-parleur barré quand muté. Raccourci global `M` (handled dans un `useEffect` au niveau `SphereApp`). Lit/écrit `useVoiceMode`.
- **`DevControls`** — Composant rendu uniquement si query `dev=1`. Contient les state pills (1-6), tweaks panel (motion / glow slider, variant select, mood select, theme select, autoCycle toggle), et le keyboard handler 1-6 force-state. Stockage dans `localStorage` pour persistance des tweaks.
- **`SphereApp`** — Composition root. Remplace le placeholder `SphereUI` actuel. Wires : `SphereCanvas` (state ← `useSphereState`, audioLevel ← `useAudioLevel`, motion/glow/variant/mood/theme ← `DevControls` ou defaults locked), `HudTasks`, `TranscriptLine`, `InputField`, `MuteToggle`, conditionally `MarkdownOverlay`, conditionally `DevControls`.

### Stylesheet et thème

- **`frontend/src/styles/hud.css`** — Global stylesheet importé en tête de `main.tsx`. Port direct du `<style>` du mockup `Bob - Sphere Lab.html`. Définit CSS vars `--bg`, `--bg-2`, `--ink`, `--ink-dim`, `--ink-faint`, `--accent`, `--accent-2`, `--accent-3`, `--warn`, `--err`, `--hud-rule`, `--hud-rule-dim`, `--hud-fill`, `--font-sans`, `--font-mono`. Sélecteurs `.theme-warm`, `.mood-calm`, `.state-alert`, `.state-error` portés. Toutes les classes `.hud-*`, `.sphere-stage`, `.glyph-overlay`, `.overlay-card`, `.md-*` portées.
- **`@theme`** Tailwind v4 — Dans `frontend/src/index.css`, déclarer un bloc `@theme` qui mappe les CSS vars vers les tokens Tailwind (`--color-accent: var(--accent)`, etc.). Permet `class="bg-accent text-ink"` pour les composants simples.
- **Google Fonts** — Import direct dans `hud.css` (`@import url(...)`) ou link dans `index.html`. Familles : `Space Grotesk`, `JetBrains Mono`, `Geist`, `Geist Mono`, `Newsreader` (toutes celles du mockup, même si V1 n'en utilise que 2-3).

### Modifications composants existants

- **`audioPlayer.ts`** — Ajouter un `getAnalyser(): AnalyserNode | null` (ou un `subscribeAudioLevel(callback)` selon ce qui colle mieux à l'architecture interne). Patch minimal du graphe Web Audio pour insérer un `AnalyserNode` entre la source TTS et la destination.
- **`App.tsx`** — Reste avec son routing `?ui=` actuel. Default flip vers `new` une fois la nouvelle UI prête (toggle dans une issue séparée à la fin).
- **`tauri.conf.json`** — Déjà patché : 2 fenêtres (`legacy` + `new`). Ajouter en fin de feature : `decorations: false` sur la fenêtre `new` une fois la nouvelle UI stable. Drag region CSS dans `hud.css` (`-webkit-app-region: drag` sur les ~28px top).
- **`scripts/dev.sh`** — Déjà patché : lance backend + Tauri (qui ouvre les 2 fenêtres natives). Aucun changement supplémentaire.

### Suppressions (en fin de feature, quand new prend le défaut)

- `frontend/src/components/ChatView.tsx`
- `frontend/src/components/ChatMessageBlock.tsx`
- `frontend/src/components/TaskCard.tsx`
- `frontend/src/components/TaskSidebar.tsx`
- `frontend/src/components/TaskDrawer.tsx`
- `frontend/src/components/Dispatcher.tsx`
- `frontend/src/components/registry.ts`

Tout autre usage de ces fichiers (imports, exports) est purgé.

### Architecture client-derive state

`useSphereState` lit le store et applique cette priorité :

1. Si `connectionStatus !== 'open'` → `'error'`
2. Sinon si `isWaitingResponse === true` ET pas encore de chunk reçu → `'think'`
3. Sinon si `speakingMsgId !== null` OU stream texte assistant en cours → `'speak'`
4. Sinon → `'idle'`

Le state retourné est consommé par `SphereCanvas` via prop ; le canvas applique une crossfade interne (~250ms) entre l'état courant et le nouveau via `stateWeightsRef` (porté du mockup `sphere.jsx`).

### Heuristique overlay markdown

`shouldOverlayResponse(content)` :
- Lignes > 3 → `true`
- Présence d'au moins un de : `^#{1,6}\s`, `^\s*[-*]\s`, `^\s*\d+\.\s`, ` ``` `, `^\s*>\s`, `\|.*\|`, `[.+](.+)`, `^\s*---\s*$` → `true`
- Sinon → `false`

Le déclenchement est fait dans `SphereApp` quand un message assistant termine son stream (event `done` ou similaire). Une seule overlay active à la fois : nouvelle réponse overlay-worthy → cross-fade du contenu in-place (même card, animation fade-out → fade-in du body uniquement, header reste).

### Dev mode

URL query `?ui=new&dev=1` :
- `DevControls` mounted
- `localStorage` pour persistance des tweaks (motion / glow / variant / mood / theme / autoCycle)
- Keyboard listeners 1-6 force-state actifs (gated comme dans mockup : skip si `INPUT` ou `TEXTAREA` focused)

### Audio reactivity

`useAudioLevel` :
- Au mount, demande à `audioPlayer.getAnalyser()`. Si `null`, retry au prochain change de `speakingMsgId`.
- Quand AnalyserNode dispo, `getByteTimeDomainData` chaque frame → calcule RMS → expose 0..1 via `useRef` (pas state, pour éviter re-render à 60fps).
- `SphereCanvas` lit le ref dans sa boucle render et passe au shader uniform `uAudio`.

## Testing Decisions

### Stack tests

- Installer **Vitest** + **`@testing-library/react`** + **`@testing-library/jest-dom`** + **`jsdom`**.
- Ajouter `frontend/vitest.config.ts` (héritage `vite.config.ts`, env `jsdom`).
- Ajouter scripts `pnpm test`, `pnpm test:watch` dans `frontend/package.json`.
- Prior art : `backend/tests/` utilise pytest avec une convention 1 fichier par module (`test_orchestrator.py` pour `orchestrator.py`, etc.). On reproduit en frontend : `Foo.test.tsx` à côté de `Foo.tsx`, ou un dossier `__tests__/` selon ce qui colle aux conventions React 19 / Vitest.

### Critères "bon test"

- Tester le comportement externe observable, pas l'implémentation interne (pas de `expect(state.x).toBe(...)` sur du state interne d'un hook ; tester ce que le hook retourne).
- Préférer des tests pure-function (deep modules) plutôt que des tests de rendering quand possible (rendering = plus lent, plus fragile).
- Mocker au seul border externe : pour `useAudioLevel`, mock `AudioContext` / `AnalyserNode` ; pour `useSphereState`, fournir un fake store.

### Modules testés (tous)

1. **`shouldOverlayResponse(content)`** — Pure function. Cas couverts : 1 ligne plain, 3 lignes plain, 4 lignes plain, heading `#`, heading `###`, liste `-`, liste numérotée, code fence, table `| a | b |`, blockquote, lien inline, mix vide / whitespace only. Idéal pour table-driven test (vitest `test.each`).

2. **`useSphereState`** — Hook pure. Test via `renderHook` de `@testing-library/react`. Cas : tous les couples `(connectionStatus, isWaitingResponse, speakingMsgId)` → état attendu. Inclure transitions (open → close, listening → done).

3. **`useAudioLevel`** — Hook qui touche Web Audio. Mock `AudioContext` global dans setup vitest. Test : fallback à 0 si `audioPlayer.getAnalyser()` retourne `null`, échantillonnage correct quand AnalyserNode présent (RMS calculé sur un buffer connu).

4. **`SphereCanvas`** — Composant DOM. Test via `render` + queries : canvas est monté, renderer est initialisé sur mount, cleanup `cancelAnimationFrame` sur unmount. WebGL2 mocké (les contextes de jsdom ne supportent pas WebGL — utiliser une fake `getContext` qui retourne un objet stub avec les méthodes nécessaires + flag `__renderCalls` pour assertion).

5. **`MarkdownOverlay`** — Composant DOM. Test : rendu d'un sample markdown produit les bonnes classes (`.md-h1`, `.md-p`, etc.), `Esc` keydown appelle `onClose`, clic backdrop appelle `onClose`, bouton `×` appelle `onClose`, clic intérieur de la card n'appelle PAS `onClose`. Sample doit inclure heading, list, code fence, table (GFM), blockquote, lien.

6. **`HudTasks`** — Composant DOM. Test : map correctement le store `tasks` map vers le rendu (running affiche spinner, done affiche check, error affiche cross, queued affiche ring vide). Limite à 4 affichés (mockup `slice(-4)`). Count badge affiche `running/total`.

7. **`TranscriptLine`** — Composant DOM. Test : affiche le hint si pas de message, affiche le dernier user pendant `think`, affiche le snippet assistant pendant/après `speak`, fade animations testées via classes CSS présentes/absentes (pas via timing).

8. **`InputField`** — Composant DOM. Test : Enter envoie la valeur via callback prop / store action, Shift+Enter ne soumet pas (newline), value vidée après submit, disabled pendant `think`/`speak` n'empêche pas de taper un follow-up.

9. **`MuteToggle`** — Composant DOM. Test : clic toggle `voiceEnabled`, touche `M` toggle aussi, icône change selon état. Skip handler si focus dans `INPUT`/`TEXTAREA`.

10. **`DevControls`** — Composant DOM. Test : rendu uniquement si `?dev=1`, state pills click force le state via callback, tweaks slider met à jour la valeur, persistance `localStorage` lue au mount.

11. **`SphereApp`** — Composition root. Test minimal : se monte sans erreur avec un store vide, route ouvre `MarkdownOverlay` quand `shouldOverlayResponse` est `true` sur le dernier message assistant.

### Prior art

- Pas de test frontend dans le repo aujourd'hui (zéro `*.test.ts` côté `frontend/`).
- Backend : `backend/tests/test_orchestrator.py` (39KB) est un bon exemple de test exhaustif d'un module deep (multi-turn loop + state machine + edge cases). On vise le même niveau de couverture sur `useSphereState`, `shouldOverlayResponse`, `useAudioLevel` côté frontend.

## Out of Scope

- **Reconnaissance vocale (speech-to-text)** : pas dans cette PRD. Le user tape en text input bas. La feature voix d'input arrivera après.
- **5 autres variants sphère** (`swarm` / `wire` / `plasma` / `void` / `glyph`) en production. Compilés dans le shader (porté tel quel) mais lock V1 sur `liquid`. Switch en dev uniquement.
- **6 surfaces overlay** autres que markdown (`email` / `image` / `video` / `map` / `doc` / `contact`). Le mockup les contient mais V1 n'expose que markdown. Les autres reviennent quand un cas d'usage backend les justifie.
- **Identity TL** (`BOB ⌬ /0.3a` + time + session timer). Pas V1. Reviendra avec V2 du HUD.
- **Telemetry TR** (`CPU / GPU / MEM / LAT`). Pas V1. Demande un canal Tauri / `sysinfo` côté Rust qui n'existe pas — overkill MVP.
- **State rail L** (`STAND BY / LISTENING / PROCESSING …` vertical). Pas V1. Le label texte est redondant avec l'animation sphère.
- **Signal tickscale R** (audio level bars). Pas V1. La sphère elle-même est audio-réactive — un deuxième indicateur visuel n'est pas justifié.
- **Diag BR** (`OBSERVER / CHANNEL / MODEL / STATE`). Pas V1. Info utile en debug mais pas en prod.
- **Frame corners HUD** (`.hud-frame` TL/TR/BL/BR). Pas V1 — la Tauri borderless les rendra naturels en V2 quand on ré-introduira le chrome.
- **Surface picker** (pills `0 NONE · 7 MAIL · 8 IMAGE …`). Pas V1 — une seule surface (markdown) existe, le picker n'a aucun sens. Reviendra avec V2 multi-surfaces.
- **WebSocket protocol changes** : aucun. Le client dérive tous les états des messages existants. Si plus tard on veut une notion d'`alert` proactif (mockup state `alert`), une PRD séparée le couvrira.
- **WebGL fallback** : si WebGL2 indispo, on affiche une bannière d'erreur HUD-style. Pas de fallback CSS/SVG (Tauri ship Chromium/WebKit moderne — cas marginal).
- **Persistance dev tweaks autre que `localStorage`** : pas de sync, pas de cloud. C'est dev only.
- **Animation crossfade entre thèmes / moods en runtime** : V1 lock warm/calm. Si dev switch en `?dev=1`, accepte un flash visuel — pas de polish requis.

## Further Notes

- Le **dev setup** est déjà en place : `scripts/dev.sh` lance backend + Tauri (qui ouvre 2 fenêtres : `legacy` 900×700 + `new` 1280×800 via `tauri.conf.json`). `frontend/public/debug.html` (split iframe browser-side) reste disponible comme fallback secondaire si on veut un compare browser avec devtools complets.
- **L'`App.tsx` actuel route déjà `?ui=`** : `?ui=legacy` (et défaut) → `<ChatView />`, `?ui=new` → `<SphereUI />` placeholder. La placeholder sera substituée par la vraie `SphereApp` au fil des issues.
- **Pas de feature flag backend** : tout vit côté frontend via la query string. Simplicité maximale.
- **Ordre d'implémentation suggéré** (à raffiner via `/to-issues`) : (1) install Vitest + setup tests, (2) port `hud.css` + tokens Tailwind, (3) `SphereCanvas` + shader, (4) `useSphereState` + tests, (5) `shouldOverlayResponse` + tests, (6) `MarkdownOverlay` + tests, (7) `useAudioLevel` + audioPlayer tap + tests, (8) `InputField` + `TranscriptLine`, (9) `HudTasks` bind store, (10) `MuteToggle`, (11) `DevControls`, (12) `SphereApp` composition + e2e smoke, (13) Tauri borderless + drag region, (14) flip default `?ui=new`, (15) suppression composants legacy.
- **Fidelity mockup max** : quand un choix se présente entre "faire propre Tailwind/React idiomatique" et "matcher exactement le mockup", on choisit le mockup. Les exceptions doivent être listées explicitement dans le PR de l'issue concernée.
- **Mockup source de vérité** vit dans `Design Mockup/` à la racine du repo (HTML + JSX + shader JS + 40+ screenshots). Ne pas le modifier — c'est la référence. Les fichiers `Design Mockup/*.jsx` ne sont JAMAIS importés par le code de prod (CDN Babel standalone, pas part of build).
