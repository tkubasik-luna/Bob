# PRD 0011 — Agent Activity Feed (live reasoning in the HUD)

## Problem Statement

Quand une tâche longue tourne (un sub-agent qui enchaîne des appels LLM, des
tool calls, des validations et des retries), l'utilisateur n'a **aucun
feedback** sur ce qui se passe. La HUD ne montre aujourd'hui qu'un état de task
(pending/running/done/failed), un `progress_status` one-liner, et le résultat
final une fois la task terminée. Tout le raisonnement intermédiaire, les
décisions d'orchestration de Jarvis, les appels d'outils, les détections de
stall ou de cap atteint, restent **invisibles** — ils ne sont émis que vers la
DebugView dev-only (`/ws/debug`, Cmd+Shift+D).

Résultat : pendant une tâche longue, l'utilisateur regarde une sphère muette
sans savoir si l'agent réfléchit, attend, est bloqué, ou a échoué. L'expérience
manque du sentiment de « voir l'IA penser » qu'on a sur un chat LLM moderne.

## Solution

Un **feed d'activité live**, partie intégrante de la HUD, qui montre en temps
réel ce que font les agents — comme la « réflexion » visible d'un chat LLM.

- Le feed vit dans un **panneau latéral collapsable** (rail étroit au repos
  avec badges des agents actifs + compteur ; auto-déplié dès qu'une activité
  démarre). La sphère reste centrale.
- Le feed **remplace le TaskSidebar et le TaskDrawer** : il devient la surface
  unique des tasks — état, activité live et accès au résultat fusionnés.
- Le feed est composé de **blocs groupés par agent** : un bloc pour Jarvis, un
  bloc par sub-task concurrente. Chaque bloc déroule sa propre réflexion.
- La réflexion s'écrit **token par token** en exploitant le canal `reasoning`
  natif des modèles (`reasoning_content`). Si le modèle/endpoint n'émet pas ce
  canal, le feed **dégrade automatiquement** vers des steps narrés dérivés des
  events existants — il n'est jamais vide.
- Au milieu du reasoning streamé, les actions discrètes (tool call, ask_user,
  stall, cap, retry, échec de validation) s'insèrent comme des **chips inline**
  dans le même fil chronologique.
- Le bloc actif affiche une **fenêtre glissante auto-scroll** du reasoning
  (hauteur bornée) avec un dropdown « voir tout ».
- Quand une task termine, son bloc **collapse en résumé** (titre + état final +
  bouton vers le résultat + dépli pour relire la réflexion). Les overlays
  résultat existantes (Mail / Sections) sont conservées et ouvertes depuis ce
  bouton.
- Le feed garde **toute la session** en scrollback ; au reload, il rehydrate
  depuis le `TaskStore` persisté (état + résumé + résultat ; le reasoning live
  d'une task déjà terminée n'est pas re-streamé).

## User Stories

1. En tant qu'utilisateur, quand je lance une tâche longue, je veux voir
   immédiatement une activité dans la HUD, afin de savoir que l'agent a démarré.
2. En tant qu'utilisateur, je veux voir la réflexion de l'agent s'écrire en
   temps réel (token par token), afin de comprendre ce qu'il est en train de
   raisonner.
3. En tant qu'utilisateur, je veux voir quels outils l'agent appelle (ex.
   recherche Gmail) et leur statut, afin de suivre concrètement son travail.
4. En tant qu'utilisateur, je veux que le feed reste lisible quand plusieurs
   sub-tasks tournent en parallèle, afin de distinguer qui fait quoi (un bloc
   par agent).
5. En tant qu'utilisateur, je veux voir le raisonnement et les décisions
   d'orchestration de Jarvis (à qui il délègue, quand il synthétise), afin de
   comprendre comment il pilote les sub-agents.
6. En tant qu'utilisateur, je veux voir dans le bloc Jarvis sa réponse finale en
   texte, afin de garder une trace écrite de ce qu'il a dit.
7. En tant qu'utilisateur, quand un agent est bloqué (stall détecté, cap
   atteint, retry), je veux le voir signalé dans le feed, afin de comprendre
   pourquoi une tâche traîne ou se dégrade.
8. En tant qu'utilisateur, quand une validation d'action échoue et est
   re-tentée, je veux le voir, afin de comprendre les ratés du modèle.
9. En tant qu'utilisateur, je ne veux PAS être noyé sous les validations qui
   passent, afin de garder un feed focalisé sur l'utile et les problèmes.
10. En tant qu'utilisateur, quand une task termine, je veux que son bloc se
    réduise en résumé, afin que le feed ne soit pas encombré par les tasks
    finies.
11. En tant qu'utilisateur, je veux pouvoir déplier un bloc terminé pour relire
    toute sa réflexion et ses actions, afin de comprendre après coup comment le
    résultat a été obtenu.
12. En tant qu'utilisateur, je veux un bouton vers le résultat (overlay Mail /
    Sections) depuis un bloc terminé, afin d'accéder au livrable.
13. En tant qu'utilisateur, je veux pouvoir réduire le feed en un rail étroit,
    afin de garder la sphère centrale dégagée quand je ne suis pas l'activité.
14. En tant qu'utilisateur, je veux que le rail collapsé montre des badges des
    agents actifs + un compteur, afin de savoir qu'il se passe quelque chose
    sans déplier.
15. En tant qu'utilisateur, je veux que le panneau se déplie automatiquement
    quand une activité démarre, afin de ne rien manquer.
16. En tant qu'utilisateur, je veux que le bloc actif n'occupe pas tout l'écran
    même si le raisonnement est long (fenêtre glissante), afin de garder les
    autres blocs visibles.
17. En tant qu'utilisateur, je veux pouvoir « voir tout » le reasoning d'un bloc
    actif via un dropdown, afin d'accéder au texte complet si besoin.
18. En tant qu'utilisateur sur un modèle local sans canal reasoning, je veux
    quand même voir des steps narrés (thoughts, tool calls, incidents), afin que
    le feed reste utile quel que soit le modèle.
19. En tant qu'utilisateur, je veux que le feed garde l'historique de toute la
    session, afin de pouvoir scroller en arrière sur les tasks précédentes.
20. En tant qu'utilisateur, quand je recharge l'app, je veux retrouver les
    tasks et leurs résumés/résultats, afin de ne pas perdre le fil.
21. En tant qu'utilisateur en mode voix, je veux que le feed coexiste avec la
    parole TTS et la sphère, afin de lire l'activité pendant que Jarvis parle.
22. En tant que développeur, je veux que le feed soit alimenté par des events
    user-facing dédiés et curatés, afin de ne pas coupler la UI au format debug
    verbeux.
23. En tant que développeur, je veux que streamer le reasoning n'altère PAS la
    validation guided-JSON de l'action du sub-agent, afin de préserver la
    correctness.
24. En tant que développeur, je veux que le feed dégrade proprement plutôt que
    de planter quand un endpoint n'expose pas le canal reasoning, afin de tenir
    la barre de robustesse.

## Implementation Decisions

### Architecture générale

- **Source de vérité / transport** : nouveaux events **user-facing dédiés**
  émis sur `/ws/chat`, distincts du ring buffer debug. Deux familles :
  - un event de **delta de reasoning** par agent (`reasoning_delta`-like) :
    `{agent_ref, delta}` où `agent_ref` identifie Jarvis (`"jarvis"`) ou une
    sub-task (`task_id`).
  - un event d'**activité discrète** (`agent_activity`-like) portant un
    descripteur de step/chip : `{agent_ref, kind, label, status, ...}` avec
    `kind ∈ {tool_call, ask_user, stall, cap, retry, validation_failed,
    started, finished}`.
  - les events de cycle de vie existants (`task_created`, `task_updated`,
    `task_result`) restent la source de l'**état** et du **résultat** des blocs.
- Les events sont **curatés** : la taxonomie de chips n'expose que tool calls +
  ask_user + incidents saillants (stall, cap, retry, validation fail). Les
  validations OK sont **discrètes/agrégées** (pas une chip par validation).
- La **redaction** appliquée au debug (Mail subject/snippet) est réappliquée sur
  ces events user-facing là où nécessaire, en réutilisant la frontière de
  redaction existante.

### Backend — streaming reasoning

- **Module `ReasoningStreamReader`** (deep) : encapsule un appel LLM sub-agent
  **streamé** et sépare deux canaux du flux :
  - le canal `reasoning_content` (deltas) → émis comme `reasoning_delta` vers le
    feed, purement cosmétique/observabilité.
  - le canal `content` (agrégé jusqu'à la fin) → reste la source de l'action.
  - **L'action est toujours parsée/validée depuis le content final agrégé**
    (guided-JSON intact). Streamer le reasoning n'a **zéro impact** sur la
    correctness ou la validation.
  - Détecte l'**absence** de canal reasoning et signale le mode dégradé.
- **Fallback** : si aucun `reasoning_content` n'est émis par l'endpoint, le feed
  est alimenté par les **steps narrés** dérivés des events existants (progress
  `thought`, tool call, validation, stall, cap). Le bloc reste vivant, sans
  texte streamé. Le choix stream-vs-narré est par-agent / par-itération, pas
  global.
- **`SubAgentRunner`** : la boucle d'itération passe du `chat(schema=...)`
  non-streamé à un chemin streamé via `ReasoningStreamReader`. Le contrat de
  sortie (SubAgentAction validée) est inchangé. Les caps (iteration, wall-clock,
  token), la détection de stall et les nudges sont préservés et émettent leurs
  chips.
- **`Orchestrator`** : émet l'activité du **bloc Jarvis** — reasoning streamé
  (même canal natif), chips d'orchestration (décision de déléguer, choix
  d'outil, synthèse), et la **réponse finale dupliquée** en texte (en plus du
  `speech_delta` qui continue d'alimenter sphère/TTS). La parole reste sur la
  sphère ; le bloc Jarvis ajoute le texte écrit.
- **`StreamChunk`** (shallow) : ajout d'un kind `reasoning` + champ
  `reasoning_delta`, en cohérence avec les kinds `tool_call_*` / `text`
  existants. `LMStudioClient.stream_complete` lit `delta.reasoning_content`
  (OpenAI-compatible) et émet ces chunks.
- **Module `ActivityProjector`** (deep, fonction pure) : projette les events
  internes (deltas reasoning + actions runner/orchestrator) en events
  user-facing curatés, applique la taxonomie de chips et la redaction. Aucune
  dépendance WS — testable en isolation.

### Frontend — feed UI

- **`activityFeedStore`** (deep, réducteur pur Zustand) : agrège les
  `reasoning_delta` par `agent_ref` en lanes, applique les chips `agent_activity`
  dans l'ordre chronologique, gère le collapse à la fin d'une task, la rétention
  session (scrollback) et le **rehydrate** depuis le snapshot `TaskStore` au
  reload (état + résumé + résultat ; pas de re-stream du reasoning passé).
- **`AgentActivityPanel`** : panneau latéral collapsable en rail (badges agents
  actifs + compteur), auto-dépli sur activité. **Remplace `TaskSidebar`.**
- **`AgentBlock`** : bloc par agent. Actif → fenêtre glissante auto-scroll du
  reasoning + chips inline entrelacés + dropdown « voir tout ». Terminé →
  collapse résumé (titre, état, bouton résultat, dépli relire). **Remplace
  `TaskDrawer`** (toute la vue détaillée vit dans le bloc expand).
- **Overlays résultat** (Mail / Sections) : conservées, ouvertes depuis le
  bouton « résultat » du bloc collapsé (via le `result_payload` existant).
- **Suppressions** : `TaskSidebar`, `TaskCard` (absorbé par `AgentBlock`),
  `TaskDrawer` et son flux `request_task_messages`/`task_messages_snapshot` si
  plus consommé.

### Concurrence & débit

- Plusieurs lanes peuvent streamer en parallèle. Les deltas reasoning sont
  **throttlés/batchés** côté émission (ou côté store) pour borner le débit WS et
  le coût de rendu React (coalescing par tick).
- Le rail collapsé agrège un compteur d'agents actifs ; chaque lane garde son
  identité (`agent_ref`).

## Testing Decisions

Un bon test vérifie le **comportement externe observable** d'un module via son
interface publique, pas ses détails d'implémentation. On script un fake LLM
client (prior art : `FakeLLMClient` qui scripte des séquences de chunks, déjà
utilisé pour le streaming de Jarvis) et on assert sur les sorties (events émis,
action validée, contenu du store). Prior art existant : `test_sub_agent_v2_runner.py`,
les tests de `stream_emitter`, et les tests de projection de deliverable.

Modules testés (sélection) :

1. **Reasoning stream + fallback (`ReasoningStreamReader`)** — backend.
   - Avec un fake émettant `reasoning_content` : les deltas reasoning sont
     exposés dans l'ordre, et le content final reste séparé.
   - Sans `reasoning_content` : le reader signale le mode dégradé et n'émet pas
     de reasoning ; le feed bascule sur steps narrés.
   - Caps et stall : vérifier que les chips d'incident sont produites.
2. **Action-from-final-content intact** — backend.
   - Avec reasoning streamé en parallèle, l'action est parsée/validée
     **uniquement** depuis le content final agrégé ; un reasoning bruité ne
     contamine pas le parsing. La validation guided-JSON et les retries se
     comportent comme avant le streaming (test de non-régression).
3. **Projection events → feed (`ActivityProjector`)** — backend.
   - Les events internes produisent les bons events user-facing (taxonomie de
     chips correcte : tool_call/ask_user/stall/cap/retry/validation_failed).
   - Les validations OK ne génèrent pas une chip chacune (agrégation/discret).
   - La redaction (Mail subject/snippet) est appliquée sur le canal user-facing.
4. **Store frontend / rehydrate (`activityFeedStore`)** — frontend.
   - Les deltas sont agrégés par `agent_ref` en lanes distinctes.
   - La fin d'une task collapse son bloc en conservant résumé + accès résultat.
   - Le rehydrate depuis un snapshot `TaskStore` reconstruit les blocs terminés
     (état/résumé/résultat) sans reasoning live.

Hors tests automatisés : rendu visuel (`AgentActivityPanel`, `AgentBlock`),
animations fenêtre glissante / auto-scroll, layout vs sphère, collapse du rail —
**vérification manuelle**.

## Out of Scope

- Persistance du **reasoning live token-par-token** d'une task terminée pour
  re-streaming au reload (on ne garde que résumé/état/résultat).
- Édition / interaction utilisateur sur le raisonnement (ex. corriger, guider
  l'agent en cours de route).
- Refonte du protocole de tool-calling ou du codec guided-JSON.
- Ajout d'un canal reasoning à des modèles qui n'en émettent pas (on dépend du
  support endpoint ; sinon fallback steps narrés).
- Lecture TTS du contenu du feed (le feed est visuel ; la parole reste le canal
  `speech_delta` / sphère existant).
- Vue groupée debug (`/ws/debug`, features 0005/0006) : conservée telle quelle,
  indépendante du feed user-facing.

## Further Notes

- **Dépendance endpoint** : le support de `reasoning_content` côté LM Studio /
  endpoint OpenAI-compatible doit être vérifié tôt ; le fallback steps narrés
  est le filet de robustesse et doit être testé en premier.
- **Cohabitation** : le feed remplace `TaskSidebar`/`TaskDrawer` mais réutilise
  le `chatStore` (tasks, taskMessages) comme source d'état et les overlays
  Mail/Sections comme surface de résultat.
- **Throttling** : prévoir un coalescing des deltas (par tick d'animation) dès
  la première version pour éviter le flood WS / re-render sous concurrence.
- **Migration douce possible** : on peut introduire le feed derrière le rail et
  retirer `TaskSidebar`/`TaskDrawer` dans le même lot, ou en deux temps si le
  découpage en issues le justifie (voir `/to-issues`).
- S'inscrit dans la lignée des features 0009 (tool-result store) et 0010
  (adaptive composite UI) : le feed consomme l'état/résultat déjà projeté et y
  ajoute la couche d'observabilité live.
