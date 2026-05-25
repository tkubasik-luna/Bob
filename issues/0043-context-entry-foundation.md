## Parent

`prd/0006-jarvis-v2-context-overhaul.md`

## What to build

Introduce the foundational types and infrastructure that the rest of the Jarvis v2 overhaul builds on, with zero observable behavior change.

Add a `ContextEntry` dataclass with the full field set fixed upfront (`id`, `kind`, `source`, `token_estimate`, `pinned`, `created_at`, `provider_id`, `payload`, `schema_version`). Add a `ContextProvider` protocol, a `ContextPolicy` config object, and a pure `ContextAssembler` that composes a list of providers into a prompt. Implement one provider — `LegacyFullHistoryProvider` — that reproduces today's full-history-every-turn behavior exactly.

Wire the orchestrator to invoke `ContextAssembler` instead of directly reading from `jarvis_store`, using policy `legacy_full_history`. Add a one-shot migration shim that maps existing `jarvis_messages` rows into `ContextEntry` form (in-place column adds + backfill), preserving order and content.

Set up the test infrastructure that will be replayed at every later stage: a golden-prompt snapshot harness for assembled prompts, and a contract-test harness for fake-LLM-driven orchestrator integration tests.

This slice is foundational scaffolding. It does not improve user-facing behavior on its own, but every later slice depends on these contracts being correct from day one.

## Acceptance criteria

- [ ] `ContextEntry` dataclass exists with all fields from PRD; `schema_version = 1`.
- [ ] `ContextProvider` protocol + `ContextPolicy` config object exist.
- [ ] `ContextAssembler` composes providers into a prompt as a pure function (no side effects, no I/O).
- [ ] `LegacyFullHistoryProvider` reproduces today's prompt verbatim (golden snapshot proves byte-equal output on a fixed transcript).
- [ ] `jarvis_messages` rows migrated to populated `ContextEntry`s; migration is idempotent and reversible.
- [ ] Orchestrator runs through `ContextAssembler` end-to-end; no production code path reads `jarvis_store` directly anymore.
- [ ] Golden-prompt snapshot test harness in CI.
- [ ] Contract-test harness (scripted fake LLM client) in CI.
- [ ] Pure-function unit tests for `ContextAssembler` and `ContextPolicy` parsing.
- [ ] Existing user-facing behavior unchanged: full Bob smoke test passes identically before and after.

## Blocked by

None - can start immediately.
