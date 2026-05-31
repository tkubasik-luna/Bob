## Parent

prd/0011-agent-activity-feed.md

## What to build

Robustesse : quand l'endpoint/modèle n'émet AUCUN canal `reasoning_content` (cas
fréquent en local), le feed ne reste pas vide — il dégrade vers des **steps
narrés** dérivés des events existants (progress `thought`, tool call,
validation, stall, cap).

- `ReasoningStreamReader` détecte l'absence de canal reasoning sur un appel et
  signale le **mode dégradé** pour cet agent / cette itération (par-agent, pas
  global).
- En mode dégradé, le bloc de l'agent reste vivant et alimenté par les steps
  narrés (pas de texte streamé token-par-token, mais un fil d'étapes lisibles).
- La bascule stream ↔ narré est transparente côté frontend : le `AgentBlock`
  affiche soit le reasoning streamé, soit les steps narrés.

## Acceptance criteria

- [ ] Avec un fake LLM qui n'émet pas `reasoning_content`, `ReasoningStreamReader`
      signale le mode dégradé et n'émet aucun delta reasoning.
- [ ] Le feed reste alimenté (thoughts / tool calls) en mode dégradé — jamais vide.
- [ ] La bascule est par-agent / par-itération (un agent dégradé n'empêche pas
      un autre de streamer son reasoning).
- [ ] Tests : branche fallback (absence de canal) — détection + émission des
      steps narrés.

## Blocked by

- issues/0069-reasoning-stream-tracer.md
