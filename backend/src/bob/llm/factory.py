"""Build :class:`LLMClient` instances for the orchestrator roles.

The orchestrator (slice #0018) talks to **two** LLMs:

- The *Jarvis* client — handles the user-facing chat turn and decides whether
  to spawn a sub-agent.
- The *sub-agent* client — runs autonomously inside :class:`SubAgentRunner`.

Both can use different backends in principle (e.g. fast local LM Studio for
Jarvis, ``claude-cli`` for sub-agents). When ``JARVIS_BACKEND`` /
``SUBAGENT_BACKEND`` are unset (the common case) we fall back to
``LLM_PROVIDER`` so existing configs keep working unchanged.
"""

from __future__ import annotations

from bob.config import Settings
from bob.llm_client import ClaudeCliClient, LLMClient, LMStudioClient


def _build_for_backend(backend: str, settings: Settings) -> LLMClient:
    if backend == "claude_cli":
        return ClaudeCliClient(settings)
    if backend == "lm_studio":
        return LMStudioClient(settings)
    raise ValueError(f"Unknown LLM backend: {backend!r}")


def build_jarvis_client(settings: Settings) -> LLMClient:
    """Return the :class:`LLMClient` instance used for the Jarvis turn.

    Honours ``settings.JARVIS_BACKEND`` when set, else falls back to
    ``settings.LLM_PROVIDER``.
    """

    backend = settings.JARVIS_BACKEND or settings.LLM_PROVIDER
    return _build_for_backend(backend, settings)


def build_subagent_client(settings: Settings) -> LLMClient:
    """Return the :class:`LLMClient` instance used by :class:`SubAgentRunner`.

    Honours ``settings.SUBAGENT_BACKEND`` when set, else falls back to
    ``settings.LLM_PROVIDER``.
    """

    backend = settings.SUBAGENT_BACKEND or settings.LLM_PROVIDER
    return _build_for_backend(backend, settings)
