"""Tests for the per-role LLM client factory (PRD 0016 / issue 0106).

Each role builds a client pinned to the role's provider / base_url / model. We
assert the built client's BACKEND TYPE (LM Studio vs Claude CLI) and, for LM
Studio, that the wire ``model`` and ``base_url`` are the role's — including the
case where two roles on the same host pin different models.
"""

from __future__ import annotations

from bob.config import Settings
from bob.llm.factory import (
    ROLE_BUILDERS,
    build_jarvis_role_client,
    build_role_client,
    build_subagent_role_client,
)
from bob.llm_client import ClaudeCliClient, LMStudioClient
from bob.llm_selection_store import LLMSelection, RoleSelection


def _settings() -> Settings:
    return Settings(
        LLM_PROVIDER="lm_studio",
        LLM_BASE_URL="http://env-default:1234/v1",
        LLM_MODEL="env-model",
        LLM_API_KEY="lm-studio",
    )


def _selection() -> RoleSelection:
    return RoleSelection(
        roles={
            "jarvis": LLMSelection(
                provider="lm_studio",
                lm_model="modelA",
                context_length={},
                base_url="http://host-a:1234/v1",
            ),
            "thinker": LLMSelection(
                provider="lm_studio",
                lm_model="modelB",
                context_length={},
                base_url="http://host-a:1234/v1",  # SAME host as jarvis
            ),
            "draft": LLMSelection(
                provider="lm_studio",
                lm_model="modelC",
                context_length={},
                base_url="http://host-b:5678/v1",  # different host
            ),
            "subagent": LLMSelection(
                provider="claude_cli",
                lm_model=None,
                context_length={},
                base_url=None,
            ),
        }
    )


def test_jarvis_role_builds_lm_studio_client_with_role_model_and_url() -> None:
    client = build_jarvis_role_client(_selection(), _settings())

    assert isinstance(client, LMStudioClient)
    # Wire model is the role's pinned model, not the .env default.
    assert client._model == "modelA"
    # base_url is the role's, not the .env default.
    assert client._settings.LLM_BASE_URL == "http://host-a:1234/v1"


def test_subagent_role_builds_claude_cli_client() -> None:
    client = build_subagent_role_client(_selection(), _settings())

    assert isinstance(client, ClaudeCliClient)


def test_two_roles_same_host_route_different_models() -> None:
    """jarvis + thinker share a host but each pins its own wire model."""

    selection = _selection()
    settings = _settings()

    jarvis = build_role_client(selection, "jarvis", settings)
    thinker = build_role_client(selection, "thinker", settings)

    assert isinstance(jarvis, LMStudioClient)
    assert isinstance(thinker, LMStudioClient)
    assert (
        jarvis._settings.LLM_BASE_URL == thinker._settings.LLM_BASE_URL == "http://host-a:1234/v1"
    )
    assert jarvis._model == "modelA"
    assert thinker._model == "modelB"


def test_each_role_builds_its_declared_backend() -> None:
    selection = _selection()
    settings = _settings()

    assert isinstance(build_role_client(selection, "jarvis", settings), LMStudioClient)
    assert isinstance(build_role_client(selection, "thinker", settings), LMStudioClient)
    assert isinstance(build_role_client(selection, "draft", settings), LMStudioClient)
    assert isinstance(build_role_client(selection, "subagent", settings), ClaudeCliClient)


def test_role_builders_table_covers_all_four_roles() -> None:
    assert set(ROLE_BUILDERS) == {"jarvis", "thinker", "draft", "subagent"}


def test_unpinned_lm_studio_role_falls_back_to_env_model() -> None:
    """An lm_studio role with no pinned model keeps the .env model (no crash)."""

    selection = RoleSelection(
        roles={
            "jarvis": LLMSelection(provider="lm_studio", lm_model=None, context_length={}),
            "thinker": LLMSelection(provider="lm_studio", lm_model=None, context_length={}),
            "draft": LLMSelection(provider="lm_studio", lm_model=None, context_length={}),
            "subagent": LLMSelection(provider="lm_studio", lm_model=None, context_length={}),
        }
    )

    client = build_role_client(selection, "jarvis", _settings())

    assert isinstance(client, LMStudioClient)
    # model override is None -> the property falls back to settings.LLM_MODEL.
    assert client._model == "env-model"
