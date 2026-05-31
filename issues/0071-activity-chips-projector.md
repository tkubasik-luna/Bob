## Parent

prd/0011-agent-activity-feed.md

## What to build

Les actions discrètes d'un agent apparaissent comme **chips inline** entrelacées
chronologiquement avec le reasoning, via un module de projection curaté.

- Nouveau module `ActivityProjector` (deep, fonction pure) : projette les events
  internes (tool call start/end, ask_user, stall nudge, cap atteint, retry,
  échec de validation) en events user-facing `agent_activity` :
  `{agent_ref, kind, label, status}` avec
  `kind ∈ {tool_call, ask_user, stall, cap, retry, validation_failed, started, finished}`.
- **Taxonomie curatée** : chips pour tool calls + ask_user + incidents saillants.
  Les validations OK ne génèrent PAS une chip chacune (agrégées / discrètes).
- **Redaction** : réappliquer la frontière de redaction existante (Mail
  subject/snippet) sur ce canal user-facing.
- **Frontend** : le `AgentBlock` insère les chips dans le même fil chronologique
  que le reasoning streamé (icône + label + statut).

## Acceptance criteria

- [ ] Les events internes produisent les bons `agent_activity` (taxonomie de
      chips correcte).
- [ ] Les validations qui passent ne génèrent pas une chip chacune (agrégation /
      discret).
- [ ] Les incidents (stall, cap, retry, validation_failed) apparaissent en chips
      saillantes.
- [ ] La redaction Mail est appliquée sur les events user-facing.
- [ ] Les chips s'affichent inline, entrelacées chronologiquement avec le
      reasoning dans le bloc.
- [ ] Tests : `ActivityProjector` — taxonomie, agrégation des validations,
      redaction.

## Blocked by

- issues/0069-reasoning-stream-tracer.md
