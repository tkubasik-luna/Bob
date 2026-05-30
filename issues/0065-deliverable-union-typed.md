# P7b — Deliverable union typed + validated end-to-end

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

Make `done.ui_payload` a first-class, validated part of the sub-agent envelope. Declare it as a `Deliverable` union — `MarkdownDeliverable(str)` or `ComponentDescriptor({component, props})` — in the `SubAgentAction` schema (`actions.py`), and bump `SUB_AGENT_SCHEMA_VERSION`. The `ComponentDescriptor` validates against the **single** `ui_registry` component schema (`ui_registry.py:181` for Mail) — not a second hand-written Mail schema — so the `say` tool's UI schema and the sub-agent deliverable schema never drift. On LM Studio the union is valid by construction under guided decoding; on Claude CLI it is parse-recovered and self-corrected, exactly like `tool_call.args`. This is the "validate the envelope" principle applied to the envelope's output half.

## Acceptance criteria

- [ ] `DoneAction.ui_payload` typed as the `Deliverable` union; `SUB_AGENT_SCHEMA_VERSION` bumped
- [ ] `ComponentDescriptor` validated against the `ui_registry` schema (single source of truth — no duplicate Mail schema)
- [ ] An invalid deliverable triggers the P5 self-correction loop, not a silent drop
- [ ] Valid by construction under LM Studio guided decoding; recovered under Claude CLI
- [ ] Tests cover both deliverable variants (Markdown + component descriptor) on both backends

## Blocked by

- `issues/0059-subagent-tool-args-schema.md`
- `issues/0060-guided-json-envelope.md`
- `issues/0064-deliverable-transport-dispatch.md`
