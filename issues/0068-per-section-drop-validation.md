## Parent

`prd/0010-adaptive-composite-ui.md`

## What to build

Make section-list validation resilient: a single malformed section never blanks the whole view. The backend validates a list of section descriptors **section by section** — invalid sections (unknown component or props failing the registry schema) are dropped, valid sections are kept, and the dropped errors are reported so the self-correction loop can still surface them.

This replaces the current all-or-nothing behavior at the list level for the deliverable path, while reusing the existing per-component `oneOf` schema (no new schema).

Verifiable on its own: a list of one bad section + two good sections yields the two good sections rendered (the bad one dropped), and the validator reports the drop reason.

## Acceptance criteria

- [ ] A section-list validator returns the kept valid sections plus the list of per-section errors for dropped ones.
- [ ] A section with an unknown component is dropped and reported.
- [ ] A section with a known component but invalid props is dropped and reported; the valid siblings are kept.
- [ ] An all-valid list passes through intact and in order.
- [ ] An empty list is handled (yields empty / `None`, no error).
- [ ] Reported errors are usable as self-correction feedback (same string shape as existing validation errors).
- [ ] A deliverable with one invalid section among valid ones still renders the valid sections end-to-end (no blanked view).
- [ ] Tests cover: all-valid passthrough, single bad-props drop with valid siblings kept, unknown-component drop, empty list, error reporting shape.

## Blocked by

- `issues/0066-sections-list-pipeline-markdown.md`
