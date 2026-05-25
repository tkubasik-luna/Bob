## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Replace the current ad-hoc validation/retry behavior with a structured, per-tool, security-aware policy. Lands BEFORE streaming so the bad-JSON blast radius is bounded when streaming amplifies it in 0049.

Introduce a per-tool `RetryPolicy` table (`max_retries`, `degrade_action`, `accept_partial`). Add a versioned `ReasonCodeRegistry` shared with the frontend (i18n-ready). Carry the retry counter on a transient `CallEnvelope` in-memory — NEVER persist it to `ContextEntry`. Implement an `on_validation_exhausted(actor)` interface so Jarvis and sub-agent share one degrade contract.

Critical: validation feedback re-injected into the LLM uses a dedicated `system_validator` role (NEVER `tool` role). The offending model output is escaped/stripped before re-injection. This blocks the LLM from treating its own bad output as trusted user content (prompt-injection safety).

Implement `accept_partial` mode: drop unknown keys, validate required-only, retry only if required failed. Saves a retry round on the common "valid required + garbage optional" case.

On exhaustion: Jarvis emits a hardcoded `say(speech="Désolé, peux-tu reformuler ?")` and logs `jarvis.validation_failed`. Sub-agent emits a forced `done(status=failed, reason_code=invalid_output)` with lineage preserved.

EXPLICITLY remove the existing `response_parser` raw-text fallback that silently corrupts assistant history (foot-gun). Any validation failure now goes through `on_validation_exhausted` — no silent path.

## Acceptance criteria

- [ ] `RetryPolicy` table is per-tool (`max_retries`, `degrade_action`, `accept_partial`) and centrally configured.
- [ ] `ReasonCodeRegistry` is versioned and the frontend i18n keys are kept in sync.
- [ ] Retry counter lives on a transient `CallEnvelope`; not persisted to any `ContextEntry`.
- [ ] Validation feedback re-injected with role `system_validator` (not `tool`); the bad model output is escaped before re-injection. Asserted in a test that simulates a prompt-injection payload.
- [ ] `accept_partial` mode drops unknown keys + validates required-only; covered by a test.
- [ ] `on_validation_exhausted(actor)` interface implemented for Jarvis (emits hardcoded `say` + logs event) and sub-agent (forced `done(failed)`).
- [ ] `response_parser` raw-text fallback removed; all callers updated; smoke test confirms no silent fallback path exists (search-and-destroy: code path provably unreachable).
- [ ] Tests assert BEHAVIOR (retry occurred once, degraded reached), not error string text.
- [ ] Integration test: malformed JSON → 1 retry → success path. Second malformed JSON → degrade path.
- [ ] Integration test: unknown `task_id` → degrade with clarifying speech.
- [ ] Golden prompts updated where the `system_validator` injection or hardcoded degrade `say` is asserted.

## Blocked by

`issues/0047-unified-say-tool.md`, `issues/0045-sub-agent-contract-rewrite.md`
