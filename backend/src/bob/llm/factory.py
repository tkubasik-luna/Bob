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

from collections.abc import Callable

from bob.config import Settings
from bob.llm_client import ClaudeCliClient, LLMClient, LMStudioClient
from bob.llm_selection_store import LLMSelection, RoleSelection

#: Signature of a per-role client builder: ``(role_selection, settings) -> client``.
RoleClientBuilder = Callable[[RoleSelection, Settings], LLMClient]


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


# =============================================================================
# Per-role builders — PRD 0016 / issue 0106 (Annexe D)
# =============================================================================
#
# The realtime agent drives FOUR roles, each pinning its OWN
# provider / base_url / lm_model / context_length (see
# :class:`bob.llm_selection_store.RoleSelection`). The builders below fold the
# role's :class:`LLMSelection` into a frozen ``model_copy`` of ``settings`` and
# dispatch the backend off the role's provider. The key difference from the
# pre-0106 global builders: the backend is read STRICTLY from the role's
# ``provider`` — there is no ``JARVIS_BACKEND`` / ``SUBAGENT_BACKEND`` env
# override at the role granularity (each role's provider IS the selection), so
# ``jarvis=lm_studio`` and ``subagent=claude_cli`` can coexist in one process.
#
# Per-request model routing: ``_apply_selection`` overrides ``LLM_MODEL`` AND
# ``LLM_BASE_URL`` from the role, so the role's :class:`LMStudioClient` sends the
# role's ``model`` on every request toward the role's server — i.e. the wire
# ``model`` param is the role's pinned model, routed to the role's base_url.
# ``claude_cli`` roles ignore the LM model / base_url (the CLI has neither).


def _build_role_client(role_selection: RoleSelection, role: str, settings: Settings) -> LLMClient:
    """Build the :class:`LLMClient` for ``role`` from its per-role selection.

    Folds ``role_selection.role(role)`` into ``settings`` and dispatches the
    backend off the role's ``provider`` (NOT the global ``LLM_PROVIDER`` or the
    per-role-agnostic ``*_BACKEND`` env knobs). Shared by every public
    ``build_<role>_client`` below so the four roles are byte-identically wired.
    """

    selection = role_selection.role(role)
    effective = _apply_selection(settings, selection)
    # The role's provider is authoritative — an unset provider falls back to the
    # decoded default (``lm_studio``), never to a foreign role's backend.
    if selection.provider == "claude_cli":
        return ClaudeCliClient(effective)
    if selection.provider == "lm_studio":
        # Pin the wire ``model`` EXPLICITLY to the role's model so two roles on
        # the same LM Studio host still route each request to their own model.
        # base_url is already per-role via the folded ``LLM_BASE_URL``.
        return LMStudioClient(effective, model=selection.lm_model)
    if selection.provider == "fake":
        # PRD 0016 / issue 0098 — the attestation harness provider, at the
        # per-role granularity (the seeded role map inherits ``LLM_PROVIDER=fake``
        # under the ephemeral backend). ``role`` is threaded so role-scoped
        # scripted rules target the right client (e.g. a ``role: thinker`` rule
        # for the Thinker loop's JSON snapshot reply). Built lazily so production
        # paths never import the attest package — mirrors ``_build_for_backend``.
        from bob.attest.fake_backend import build_fake_client_from_settings

        return build_fake_client_from_settings(role)
    raise ValueError(f"Unknown LLM backend: {selection.provider!r}")


def build_jarvis_role_client(role_selection: RoleSelection, settings: Settings) -> LLMClient:
    """Return the :class:`LLMClient` for the ``jarvis`` (Speaker) role."""

    return _build_role_client(role_selection, "jarvis", settings)


def build_thinker_role_client(role_selection: RoleSelection, settings: Settings) -> LLMClient:
    """Return the :class:`LLMClient` for the ``thinker`` role.

    Wired into the map for completeness; the Thinker loop that consumes it lands
    in a later slice (S6).
    """

    return _build_role_client(role_selection, "thinker", settings)


def build_draft_role_client(role_selection: RoleSelection, settings: Settings) -> LLMClient:
    """Return the :class:`LLMClient` for the ``draft`` (speculative) role.

    Consumed by :class:`bob.speculative_draft.SpeculativeDraft` (PRD 0016 / issue
    0104): a mini fast model that pre-writes the conversational reply on the
    partial transcript while the user speaks. The ``fake`` branch routes ``role:
    draft`` scripted rules so the attest harness can drive the speculative text.
    """

    return _build_role_client(role_selection, "draft", settings)


def build_subagent_role_client(role_selection: RoleSelection, settings: Settings) -> LLMClient:
    """Return the :class:`LLMClient` for the ``subagent`` role."""

    return _build_role_client(role_selection, "subagent", settings)


#: Dispatch table ``role -> builder`` so the swap coordinator / router can build
#: exactly one role's client by name without a four-way branch.
ROLE_BUILDERS: dict[str, RoleClientBuilder] = {
    "jarvis": build_jarvis_role_client,
    "thinker": build_thinker_role_client,
    "draft": build_draft_role_client,
    "subagent": build_subagent_role_client,
}


def build_role_client(role_selection: RoleSelection, role: str, settings: Settings) -> LLMClient:
    """Build the client for ``role`` via :data:`ROLE_BUILDERS` (KeyError if unknown)."""

    return ROLE_BUILDERS[role](role_selection, settings)
