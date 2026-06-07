"""Bob attestation harness (PRD 0016 / issue 0098).

The ``bob`` CLI (:mod:`bob.attest.cli`) drives a running backend over the real
WS/HTTP and asserts machine-readable invariants on the ``/ws/debug`` stream,
rendering a verdict JSON (Annexe C). Public building blocks:

- :class:`bob.attest.ephemeral.EphemeralBackend` — isolated throwaway backend.
- :class:`bob.attest.fake_backend.FakeLlmClient` — deterministic scripted LLM
  (the ``fake`` provider).
- :class:`bob.attest.runner.ScenarioRunner` — parse YAML + execute timeline +
  emit verdict.
- :mod:`bob.attest.assertions` — extensible assertion registry.
"""

from __future__ import annotations
