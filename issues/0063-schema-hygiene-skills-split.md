# P6 — Schema hygiene + skills/tools split

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

Two hygiene moves that keep the tool layer robust as it grows. First, schema hygiene: a `flatten_schema()` step applied once at tool registration that inlines `$ref`, avoids `anyOf` where a flat string enum works, and caps nesting depth (local / OpenAI-compatible models and guided decoding choke on `anyOf` / `$ref` / deep nesting); plus deterministic ordering of tool lists before injection for prompt-cache stability. Second, the skills/tools split: extract the ~70-line French Gmail prose recipe (`prompt_fragments.py:321-385`) out of the core action prompt into a composable `SkillPack` loaded only when the goal matches, leaving `SUB_AGENT_V2_SYSTEM_PROMPT` (`:296`) focused on the action contract.

## Acceptance criteria

- [ ] `flatten_schema()` applied to every tool spec at registration; warns (does not silently drop) on lost expressiveness
- [ ] Tool lists ordered deterministically before the model payload
- [ ] Gmail recipe relocated to a `SkillPack`; base sub-agent contract no longer carries it
- [ ] Skill loaded into the prompt only when the goal matches; `gmail_search` task still works end-to-end
- [ ] Tests cover flattening, ordering stability, and conditional skill loading

## Blocked by

- `issues/0059-subagent-tool-args-schema.md`
