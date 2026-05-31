## Parent

prd/0011-agent-activity-feed.md

## What to build

Tracer bullet end-to-end : la réflexion d'une sub-task running s'écrit en direct
dans la HUD. Cible le chemin minimal complet — du canal LLM jusqu'à un bloc
d'affichage.

- **Backend** : étendre `StreamChunk` avec un kind `reasoning` (+ `reasoning_delta`).
  `LMStudioClient.stream_complete` lit `delta.reasoning_content` (OpenAI-compatible)
  et émet ces chunks. Nouveau module `ReasoningStreamReader` (deep) qui consomme un
  appel LLM sub-agent **streamé** et sépare deux canaux : `reasoning_content`
  (deltas → feed) et `content` (agrégé → action). `SubAgentRunner` passe du
  `chat(schema=...)` non-streamé à ce chemin streamé.
- **Contrat de correctness** : l'action reste parsée/validée depuis le **content
  final agrégé** (guided-JSON intact). Le reasoning est purement cosmétique.
- **Transport** : nouvel event user-facing `reasoning_delta` sur `/ws/chat` :
  `{agent_ref: task_id, delta}`.
- **Frontend** : `activityFeedStore` minimal qui agrège les deltas par `agent_ref` ;
  un `AgentBlock` basique qui rend le texte de reasoning en streaming pour une
  sub-task running (affiché de façon visible, cohabitant avec l'UI tasks actuelle).

## Acceptance criteria

- [ ] `StreamChunk` supporte le kind `reasoning` avec un champ delta dédié.
- [ ] `LMStudioClient.stream_complete` émet des chunks `reasoning` à partir de
      `delta.reasoning_content` quand l'endpoint en fournit.
- [ ] `ReasoningStreamReader` expose les deltas reasoning dans l'ordre ET le
      content final agrégé séparément.
- [ ] Le `SubAgentRunner` obtient son action validée depuis le content final ;
      le streaming du reasoning n'altère pas la validation guided-JSON (test de
      non-régression).
- [ ] Un event `reasoning_delta` est émis sur `/ws/chat` par sub-task, taggé par
      `agent_ref`.
- [ ] Pendant une sub-task longue, l'utilisateur voit le reasoning s'écrire
      token par token dans un bloc de la HUD.
- [ ] Tests : `ReasoningStreamReader` (canal reasoning présent), et
      action-from-final-content (non-régression validation).

## Blocked by

None - can start immediately.
