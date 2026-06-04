## Parent

prd/0015-mcp-tool-scaling.md

## What to build

Goal-driven tool retrieval so the sub-agent advertises only the most relevant
tools to the model instead of the entire registry. Today the runner renders the
full registry into the prompt on every turn; at scale this drowns the local
model.

Add a pure, deterministic `select_tools(registry, goal, *, k, min_score)` that
ranks tools lexically against the task goal (over `name + description + tags`,
field-weighted, accent-stripped, French stop-words removed, deterministic
tie-break by name) and returns `always_on tools ∪ {tools scoring ≥ min_score}`
capped at `k` by score — never padded with zero-score tools, and falling back to
the `always_on` core when retrieval is empty. V1 is a hand-rolled weighted
keyword score (zero dependency).

Give the sub-agent tool definition two optional fields (`always_on: bool`,
`tags: tuple[str, ...]`), tag the existing Gmail and web tools, and feed the
runner's tool-catalogue rendering the retrieved subset for the task goal. Tool
**dispatch** stays on the full registry by name — advertised set ⊂ dispatchable
set. Add config knobs for the advertised-tool cap and minimum score.

This slice has standalone value with no MCP tools: a thinner advertised surface
already improves local-model reliability.

## Acceptance criteria

- [ ] `select_tools` is pure/deterministic (no I/O, no model) and unit-tested
      table-driven: mail / web / multi-intent / junk goals each yield the exact
      expected advertised set and ordering.
- [ ] Goal "dernier mail" advertises `gmail_search` and excludes the web tools;
      goal "actu sur X" advertises `web_search`; multi-intent
      "mes mails et la météo" surfaces both relevant tools.
- [ ] `k` cap, `min_score` threshold, no-zero-score-pad rule, and empty →
      `always_on` fallback are each asserted.
- [ ] A simulated registry of ~30 tools advertises ≤ `k` tools for a focused goal.
- [ ] The runner renders only the retrieved subset into the prompt catalogue,
      while a registered-but-not-advertised tool still resolves on dispatch by
      name (integration test).
- [ ] Existing Gmail and web tools carry retrieval `tags`.
- [ ] Config exposes the advertised-tool cap and minimum relevance score.
- [ ] Existing runner / orchestrator suites stay green (zero regression).

## Blocked by

None - can start immediately.
