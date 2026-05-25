"""Shared test harnesses for the Jarvis v2 overhaul (PRD 0006).

Two utilities are exposed for cross-test reuse:

- :mod:`fake_llm` — :class:`FakeLLMClient` (scriptable
  :class:`bob.llm_client.LLMClient`) for orchestrator integration tests
  that don't want to spin up a real LM Studio backend.
- :mod:`golden_prompt` — JSON-snapshot helpers used by the
  ``test_legacy_full_history_provider`` golden-prompt suite. The snapshot
  files live under ``backend/tests/fixtures/prompts/`` and are versioned
  alongside the test code.

Both are introduced at issue 0043 and re-used by every later slice.
"""
