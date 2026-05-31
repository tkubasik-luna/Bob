## Parent

prd/0011-agent-activity-feed.md

## What to build

Jarvis a son propre bloc dans le feed (lane `agent_ref = "jarvis"`), à côté des
sub-tasks.

- **Backend** : l'`Orchestrator` émet, sur le même contrat user-facing :
  - les deltas de reasoning de Jarvis (canal `reasoning_content` natif),
  - des chips d'orchestration (`agent_activity`) : décision de déléguer, choix
    d'outil, synthèse,
  - la **réponse finale dupliquée en texte** (en plus du `speech_delta` qui
    continue d'alimenter sphère/TTS).
- **Frontend** : le `AgentBlock` Jarvis affiche reasoning + chips d'orchestration
  + le texte de la réponse finale. La parole reste sur la sphère/transcript line ;
  le bloc Jarvis ajoute la trace écrite.

## Acceptance criteria

- [ ] Une lane Jarvis apparaît dans le feed, distincte des sub-tasks.
- [ ] Le reasoning de Jarvis s'écrit en live dans son bloc (quand canal dispo ;
      sinon steps narrés via la même bascule).
- [ ] Les chips d'orchestration (délégation / synthèse) apparaissent dans le bloc
      Jarvis.
- [ ] La réponse finale de Jarvis apparaît en texte dans le bloc, sans casser le
      `speech_delta` → sphère/TTS existant.

## Blocked by

- issues/0069-reasoning-stream-tracer.md
- issues/0071-activity-chips-projector.md
