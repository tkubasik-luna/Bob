## Parent

prd/0015-mcp-tool-scaling.md

## What to build

The weather acceptance case: a real single-shot MCP tool exercised through every
layer — retrieval surfaces it, the manager calls it, the projector cards it, and
Bob speaks a French answer. This proves the whole pipeline end-to-end on full
LM Studio.

- A `weather` entry in the `mcp_servers` manifest exposing a forecast tool, with
  weather `tags`, a French `description_fr`, a narrowed `args` surface
  (place + date), and `terminal: true` (single-shot lookup converges).
- A goal-triggered weather skill pack carrying the recipe (extract place + date →
  call the forecast tool → synthesise a short French answer), registered in the
  ordered sub-agent skill-pack list (mirrors the gmail/web packs). Triggers:
  météo / temps / weather / prévision.
- A single weather capability line in Jarvis's system prompt, leaking no tool
  name (mirrors the gmail/web routing pattern), so Jarvis routes a weather ask to
  a sub-task.
- The generic Markdown card is the shipped surface; a typed `Weather` card is an
  explicit later upgrade, not in this slice.

## Acceptance criteria

- [ ] Integration test (mocked weather session): a weather goal surfaces the
      forecast tool via `select_tools`, the call runs, the projection produces a
      deliverable, and the spoken summary is a French forecast line.
- [ ] With the weather server reachable, "quel temps demain à Paris ?" routes to
      a sub-task that calls the forecast tool and converges (terminal).
- [ ] The forecast tool is advertised for a weather goal and excluded for a
      mail/web goal (retrieval gating holds with weather registered).
- [ ] Weather server unreachable → Bob speaks a French "service météo
      indisponible" sentence; no broken overlay, task marked failed.
- [ ] HITL smoke (deferred to user): asking the weather speaks a French answer
      and shows a card in the HUD dock.

## Blocked by

- issues/0092-tool-retrieval-gating.md
- issues/0094-mcp-manifest-lifecycle.md
