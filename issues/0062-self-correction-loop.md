# P5 — Self-correction loop (system_validator retry)

## Parent

`prd/0008-tool-calling-unification.md`

## What to build

When a tool call fails to parse or fails arg validation, echo the specific error back to the model and let it retry — bounded by the existing per-tool `RetryPolicy`. The error feedback is re-injected under the **`system_validator`** role, never the `tool` role (prompt-injection safety, inherited verbatim from PRD 0006). This replaces today's silent drop on the Jarvis path (`llm_client.py:1452`, `:1463`) and the whole-task force-fail on the sub-agent path (`done(failed, invalid_output)`). It is the primary safety net on the Claude CLI backend, which has no constrained decoding to prevent malformed output in the first place.

## Acceptance criteria

- [ ] Parse/validation error re-injected under the `system_validator` role with the specific error message (offending output escaped)
- [ ] Retry bounded by the existing per-tool `RetryPolicy`; exhaustion path explicit (no silent drop / no whole-task fail without feedback)
- [ ] Recovers a malformed call on both backends; on Claude CLI it recovers a case guided decoding would have prevented on LM Studio
- [ ] Security test asserts feedback never uses the `tool` role
- [ ] Tests cover recover-on-retry and retry-exhaustion

## Blocked by

- `issues/0059-subagent-tool-args-schema.md`
- `issues/0061-hermes-tool-codec.md`
