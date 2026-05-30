# P2 — Sub-agent tool args via schema

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

Stop making the sub-agent model guess argument names from prose. Inject each tool's real argument JSON Schema (via the codec, derived from `args_model`) in place of the current name+description-only lines (`runner.py:784-790`), and validate `tool_call.args` against `args_model` before dispatch. Highest-ROI slice: it is the single biggest robustness miss today, and it makes adding a sub-agent tool a matter of declaring a Pydantic model rather than writing a prose recipe.

## Acceptance criteria

- [ ] `SubAgentToolDefinition.to_spec()` derives flat `parameters` from `args_model`
- [ ] Sub-agent prompt shows the argument JSON Schema per tool, not name+description
- [ ] `tool_call.args` validated against `args_model`; invalid args produce a structured error (no silent drop)
- [ ] `gmail_search` task still works end-to-end
- [ ] Tests cover schema injection and arg validation (valid + invalid)

## Blocked by

- `issues/0058-canonical-toolspec-codec-seam.md`
