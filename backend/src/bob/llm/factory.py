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
from bob.llm_selection_store import LLMSelection


def _build_for_backend(backend: str, settings: Settings, *, role: str | None = None) -> LLMClient:
    if backend == "claude_cli":
        return ClaudeCliClient(settings)
    if backend == "lm_studio":
        return LMStudioClient(settings)
    if backend == "fake":
        # PRD 0016 / issue 0098 — the attestation harness provider. Built lazily
        # so production paths never import the attest package. ``role`` is
        # threaded through so role-scoped scripted rules (and the future
        # ``role_used_model`` assertion) can target the right client.
        from bob.attest.fake_backend import build_fake_client_from_settings

        return build_fake_client_from_settings(role)
    raise ValueError(f"Unknown LLM backend: {backend!r}")


def _apply_selection(settings: Settings, selection: LLMSelection | None) -> Settings:
    """Return ``settings`` with the runtime ``selection`` folded in.

    The factory historically built from the frozen ``.env`` :class:`Settings`.
    The live-swap path (issue 0080) must rebuild the client for a NEW model id
    chosen at runtime, so we thread the persisted :class:`LLMSelection` through
    here: a frozen ``model_copy(update=…)`` overrides ``LLM_PROVIDER`` /
    ``LLM_MODEL`` (and the model-scoped context length, when present) without
    mutating the shared settings singleton.

    ``None`` (the boot-time call when no live selection is wired yet) returns
    ``settings`` unchanged so existing behaviour is byte-for-byte preserved.
    """

    if selection is None:
        return settings
    update: dict[str, object] = {"LLM_PROVIDER": selection.provider}
    if selection.lm_model:
        update["LLM_MODEL"] = selection.lm_model
    # Runtime URL swap (picker URL field): the persisted base_url overrides the
    # frozen ``.env`` ``LLM_BASE_URL`` so the inference ``openai`` client points
    # at the chosen server. ``None`` keeps the ``.env`` value.
    if selection.base_url:
        update["LLM_BASE_URL"] = selection.base_url
    return settings.model_copy(update=update)


def build_jarvis_client(
    settings: Settings,
    selection: LLMSelection | None = None,
) -> LLMClient:
    """Return the :class:`LLMClient` instance used for the Jarvis turn.

    Honours ``settings.JARVIS_BACKEND`` when set, else falls back to the active
    provider. When a runtime ``selection`` is supplied (the live-swap path) its
    provider/model override the frozen ``.env`` values.
    """

    effective = _apply_selection(settings, selection)
    backend = effective.JARVIS_BACKEND or effective.LLM_PROVIDER
    return _build_for_backend(backend, effective, role="jarvis")


def build_subagent_client(
    settings: Settings,
    selection: LLMSelection | None = None,
) -> LLMClient:
    """Return the :class:`LLMClient` instance used by :class:`SubAgentRunner`.

    Honours ``settings.SUBAGENT_BACKEND`` when set, else falls back to the
    active provider. When a runtime ``selection`` is supplied (the live-swap
    path) its provider/model override the frozen ``.env`` values.
    """

    effective = _apply_selection(settings, selection)
    backend = effective.SUBAGENT_BACKEND or effective.LLM_PROVIDER
    return _build_for_backend(backend, effective, role="subagent")
