## Parent

prd/0012-llm-provider-model-picker.md

## What to build

Live provider switching between Claude CLI and LM Studio, end-to-end, reusing the
validate-then-swap + rebuild mechanism from the model-switch slice. Extend `PUT /api/llm/selection`
to handle a `provider` change: **validate the target before swapping** (probe LM Studio for the
LM target; verify the `claude` binary is present for the Claude CLI target), then rebuild **both**
the Jarvis and sub-agent clients via the factory using the new selection and swap the references.
On validation failure (LM Studio down / `claude` missing), keep the previous provider, do not write
JSON, return an error.

Wire the picker's segmented provider toggle to fire the `PUT`, and render the Claude side as a
read-only model label (from `CLAUDE_CLI_MODEL` or default) with no model dropdown / no
context-length control.

## Acceptance criteria

- [ ] `PUT /api/llm/selection` with a new `provider` validates the target first: LM Studio reachable for LM, `claude` binary present for Claude CLI.
- [ ] On success, both Jarvis and sub-agent clients are rebuilt from the factory with the new selection and swapped; the JSON is written.
- [ ] On validation failure, the previous provider is kept, the JSON is not written, and the endpoint returns an error.
- [ ] Picker: segmented toggle switches provider via the `PUT`; Claude side shows a read-only model label with no dropdown / no ctx slider.
- [ ] Tests: target validation pass/fail per provider; both clients rebuilt + swapped on success; previous provider retained on failure; Vitest toggle + Claude read-only label.

## Blocked by

- issues/0080-live-model-switch.md
