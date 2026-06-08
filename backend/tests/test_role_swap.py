"""Tests for the per-role swap coordinator (PRD 0016 / issue 0106).

The contract: swapping ONE role rebuilds ONLY that role's client and persists
the v2 map; the other three roles' client OBJECTS are unchanged, and a role's
sink fires with the rebuilt client. Validation (unknown role / provider) happens
before any rebuild / write.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from bob.config import Settings
from bob.llm_client import ClaudeCliClient, LLMClient, LMStudioClient
from bob.llm_selection_store import (
    ROLES,
    LLMSelection,
    RoleSelection,
    RoleSelectionStore,
)
from bob.llm_swap import (
    RoleClientRegistry,
    RoleLLMSwitcher,
    RoleManagerRegistry,
    UnknownProviderError,
)

from ._harness.fake_llm import FakeLLMClient


def _settings() -> Settings:
    return Settings(
        LLM_PROVIDER="lm_studio",
        LLM_BASE_URL="http://localhost:1234/v1",
        LLM_MODEL="boot-model",
        LLM_API_KEY="lm-studio",
    )


def _seed(store: RoleSelectionStore) -> RoleSelection:
    selection = RoleSelection(
        roles={
            role: LLMSelection(
                provider="lm_studio",
                lm_model=f"{role}-model",
                context_length={},
                base_url="http://localhost:1234/v1",
            )
            for role in ROLES
        }
    )
    store.write(selection)
    return selection


def _registry_with_sinks() -> tuple[RoleClientRegistry, dict[str, list[LLMClient]]]:
    """A registry pre-seeded with one fake client per role + recording sinks."""

    fired: dict[str, list[LLMClient]] = {role: [] for role in ROLES}
    clients: dict[str, LLMClient] = {role: FakeLLMClient() for role in ROLES}

    def _make_sink(role: str) -> Callable[[LLMClient], None]:
        def _sink(client: LLMClient) -> None:
            fired[role].append(client)

        return _sink

    sinks: dict[str, Callable[[LLMClient], None]] = {role: _make_sink(role) for role in ROLES}
    return RoleClientRegistry(clients, sinks=sinks), fired


def _switcher(
    tmp_path: Path,
) -> tuple[RoleLLMSwitcher, RoleClientRegistry, dict[str, list[LLMClient]], RoleSelectionStore]:
    store = RoleSelectionStore(tmp_path / "llm_selection.json")
    _seed(store)
    registry, fired = _registry_with_sinks()
    switcher = RoleLLMSwitcher(
        settings=_settings(),
        selection_store=store,
        registry=registry,
    )
    return switcher, registry, fired, store


@pytest.mark.asyncio
async def test_swap_role_rebuilds_only_that_role(tmp_path: Path) -> None:
    switcher, registry, fired, _store = _switcher(tmp_path)

    before = {role: registry.get(role) for role in ROLES}

    await switcher.swap_role(
        "jarvis",
        LLMSelection(provider="lm_studio", lm_model="new-jarvis", context_length={}),
    )

    # Only jarvis' client object changed; the others are the SAME instance.
    assert registry.get("jarvis") is not before["jarvis"]
    for role in ("thinker", "draft", "subagent"):
        assert registry.get(role) is before[role]

    # Only jarvis' sink fired (exactly once, with the rebuilt client).
    assert len(fired["jarvis"]) == 1
    assert fired["jarvis"][0] is registry.get("jarvis")
    for role in ("thinker", "draft", "subagent"):
        assert fired[role] == []


@pytest.mark.asyncio
async def test_swap_role_persists_only_that_role_in_map(tmp_path: Path) -> None:
    switcher, _registry, _fired, store = _switcher(tmp_path)

    await switcher.swap_role(
        "subagent",
        LLMSelection(provider="claude_cli", lm_model=None, context_length={}),
    )

    persisted = store.read()
    assert persisted is not None
    # subagent flipped to claude_cli; the other three round-trip unchanged.
    assert persisted.role("subagent").provider == "claude_cli"
    assert persisted.role("jarvis").lm_model == "jarvis-model"
    assert persisted.role("thinker").lm_model == "thinker-model"
    assert persisted.role("draft").lm_model == "draft-model"


@pytest.mark.asyncio
async def test_swap_role_rebuilt_client_matches_new_provider(tmp_path: Path) -> None:
    switcher, registry, _fired, _store = _switcher(tmp_path)

    await switcher.swap_role(
        "subagent",
        LLMSelection(provider="claude_cli", lm_model=None, context_length={}),
    )
    assert isinstance(registry.get("subagent"), ClaudeCliClient)

    await switcher.swap_role(
        "jarvis",
        LLMSelection(
            provider="lm_studio",
            lm_model="routed-model",
            context_length={},
            base_url="http://host-x:1234/v1",
        ),
    )
    jarvis = registry.get("jarvis")
    assert isinstance(jarvis, LMStudioClient)
    assert jarvis._model == "routed-model"
    assert jarvis._settings.LLM_BASE_URL == "http://host-x:1234/v1"


@pytest.mark.asyncio
async def test_swap_unknown_role_rejected_before_any_mutation(tmp_path: Path) -> None:
    switcher, registry, fired, store = _switcher(tmp_path)
    before = {role: registry.get(role) for role in ROLES}

    with pytest.raises(UnknownProviderError):
        await switcher.swap_role(
            "speaker",  # not one of the four
            LLMSelection(provider="lm_studio", lm_model="x", context_length={}),
        )

    # Nothing rebuilt / fired / written.
    for role in ROLES:
        assert registry.get(role) is before[role]
        assert fired[role] == []
    assert store.read().role("jarvis").lm_model == "jarvis-model"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_swap_unknown_provider_rejected_before_any_mutation(tmp_path: Path) -> None:
    switcher, registry, fired, _store = _switcher(tmp_path)
    before = {role: registry.get(role) for role in ROLES}

    with pytest.raises(UnknownProviderError):
        await switcher.swap_role(
            "jarvis",
            LLMSelection(provider="gpt5", lm_model="x", context_length={}),
        )

    for role in ROLES:
        assert registry.get(role) is before[role]
        assert fired[role] == []


@pytest.mark.asyncio
async def test_swap_role_serialised_under_lock(tmp_path: Path) -> None:
    """Concurrent per-role swaps run one-at-a-time; the last write per role wins."""

    switcher, _registry, _fired, store = _switcher(tmp_path)

    await asyncio.gather(
        switcher.swap_role(
            "jarvis", LLMSelection(provider="lm_studio", lm_model="m1", context_length={})
        ),
        switcher.swap_role(
            "draft", LLMSelection(provider="lm_studio", lm_model="m2", context_length={})
        ),
    )

    persisted = store.read()
    assert persisted is not None
    # Both swaps landed on a coherent map (no lost update of the other role).
    assert persisted.role("jarvis").lm_model == "m1"
    assert persisted.role("draft").lm_model == "m2"


@pytest.mark.asyncio
async def test_swap_role_without_seed_uses_defaults(tmp_path: Path) -> None:
    """Swapping before the store is seeded still works (fallback seed)."""

    store = RoleSelectionStore(tmp_path / "llm_selection.json")  # not seeded
    registry, _fired = _registry_with_sinks()
    switcher = RoleLLMSwitcher(settings=_settings(), selection_store=store, registry=registry)

    updated = await switcher.swap_role(
        "jarvis", LLMSelection(provider="claude_cli", lm_model=None, context_length={})
    )

    assert updated.role("jarvis").provider == "claude_cli"
    # The other roles seeded from .env defaults.
    assert updated.role("thinker").provider == "lm_studio"
    assert cast(RoleSelection, store.read()).role("jarvis").provider == "claude_cli"


# --- set_reasoning: per-request param, no model reload -----------------------


@pytest.mark.asyncio
async def test_set_reasoning_persists_and_keeps_model(tmp_path: Path) -> None:
    """Reasoning update persists the level + keeps the role's model/base_url."""

    switcher, registry, fired, store = _switcher(tmp_path)

    updated = await switcher.set_reasoning("jarvis", "high")

    # Level persisted; provider/model/base_url untouched.
    assert updated.role("jarvis").reasoning == "high"
    assert updated.role("jarvis").lm_model == "jarvis-model"
    assert updated.role("jarvis").base_url == "http://localhost:1234/v1"
    persisted = store.read()
    assert persisted is not None
    assert persisted.role("jarvis").reasoning == "high"

    # Only jarvis' client was refreshed (so the new level rides the next request);
    # the rebuilt client carries the level. Other roles untouched.
    jarvis = registry.get("jarvis")
    assert isinstance(jarvis, LMStudioClient)
    assert jarvis._reasoning == "high"
    assert len(fired["jarvis"]) == 1
    for role in ("thinker", "draft", "subagent"):
        assert fired[role] == []


@pytest.mark.asyncio
async def test_set_reasoning_clears_with_none(tmp_path: Path) -> None:
    """Passing None clears the level (→ model's auto setting)."""

    switcher, _registry, _fired, store = _switcher(tmp_path)
    await switcher.set_reasoning("jarvis", "low")
    await switcher.set_reasoning("jarvis", None)

    assert cast(RoleSelection, store.read()).role("jarvis").reasoning is None


@pytest.mark.asyncio
async def test_set_reasoning_rejects_invalid_level(tmp_path: Path) -> None:
    """An out-of-range level is rejected before any write."""

    switcher, _registry, _fired, store = _switcher(tmp_path)
    with pytest.raises(UnknownProviderError):
        await switcher.set_reasoning("jarvis", "extreme")
    assert cast(RoleSelection, store.read()).role("jarvis").reasoning is None


@pytest.mark.asyncio
async def test_set_reasoning_rejects_unknown_role(tmp_path: Path) -> None:
    switcher, _registry, _fired, _store = _switcher(tmp_path)
    with pytest.raises(UnknownProviderError):
        await switcher.set_reasoning("speaker", "high")


@pytest.mark.asyncio
async def test_set_reasoning_never_touches_load_policy(tmp_path: Path) -> None:
    """The key contract: reasoning is request-scoped — set_reasoning must NOT run
    the per-host multi-load policy (no model load / eviction / budget check),
    even when a manager registry is wired. Any manager call would explode here."""

    store = RoleSelectionStore(tmp_path / "llm_selection.json")
    _seed(store)
    registry, _fired = _registry_with_sinks()

    class _ExplodingManager:
        def assign_role(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("assign_role must not run for a reasoning update")

        def release_role(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("release_role must not run for a reasoning update")

    manager_registry = RoleManagerRegistry(factory=lambda _host: _ExplodingManager())  # type: ignore[arg-type]
    switcher = RoleLLMSwitcher(
        settings=_settings(),
        selection_store=store,
        registry=registry,
        manager_registry=manager_registry,
    )

    # No AssertionError → the load policy was never invoked.
    updated = await switcher.set_reasoning("jarvis", "medium")
    assert updated.role("jarvis").reasoning == "medium"
