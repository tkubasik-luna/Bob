"""Integration tests for the per-role multi-load + budget refusal (issue 0107).

These wire the REAL :class:`bob.llm_swap.RoleLLMSwitcher` to a
:class:`bob.llm_swap.RoleManagerRegistry` backed by v2
:class:`bob.lm_studio_manager.LMStudioManager` instances (with the ``lmstudio``
SDK faked at the client-factory boundary, exactly as
``test_lm_studio_manager.py`` does). They are the ATTESTABLE expression of the
issue-0107 ``bob attest`` scenario, since the ``fake`` provider cannot load real
models under the harness:

- assign two roles to two DISTINCT local models → assert BOTH resident on the
  host manager (true concurrency, multi-load — no offload-first);
- assign a third model that breaks the host ceiling → assert the swap is REFUSED
  (``ModelBudgetExceededError``) and surfaces as a 409 with the "dépasse le
  plafond" message through ``PUT /api/llm/roles/{role}`` — the previous roles'
  state stands (nothing rebuilt / persisted).

What's unit-tested vs e2e-attestable is documented in the issue 0107 report.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import lmstudio
import pytest
from fastapi.testclient import TestClient

from bob import llm_router
from bob.config import Settings
from bob.llm_selection_store import ROLES, LLMSelection, RoleSelection, RoleSelectionStore
from bob.llm_swap import (
    RoleClientRegistry,
    RoleLLMSwitcher,
    RoleManagerRegistry,
)
from bob.lm_studio_manager import (
    LMStudioManager,
    ModelBudgetExceededError,
    _SDKClient,
    _SDKDownloadedModel,
    _SDKLoadedModel,
)
from bob.main import app
from bob.model_budget import HostBudget

from ._harness.fake_llm import FakeLLMClient


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LLM_PROVIDER": "lm_studio",
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_MODEL": "boot-model",
        "LLM_API_KEY": "lm-studio",
        "CLAUDE_CLI_MODEL": "claude-opus-4",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeLlm:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.unloaded: list[str] = []

    def load_new_instance(self, model_key: str, *, config: object | None = None) -> object:
        self.loaded.append(model_key)
        return object()

    def unload(self, model_identifier: str) -> None:
        self.unloaded.append(model_identifier)


class _FakeClient:
    def __init__(self, llm: _FakeLlm) -> None:
        self.llm = llm
        self.closed = False

    def list_downloaded_models(self) -> Sequence[_SDKDownloadedModel]:
        return []

    def list_loaded_models(self) -> Sequence[_SDKLoadedModel]:
        return []

    def close(self) -> None:
        self.closed = True


def _fixed_footprint(gib: float) -> Callable[[str, int | None], float]:
    def _probe(_model_id: str, _ctx: int | None) -> float:
        return gib

    return _probe


def _local_manager(
    ceiling_gib: float, footprint_gib: float = 4.0
) -> tuple[LMStudioManager, _FakeLlm]:
    llm = _FakeLlm()
    client = _FakeClient(llm)

    def _factory(_host: str) -> _SDKClient:
        return client

    manager = LMStudioManager(
        host="localhost:1234",
        client_factory=_factory,
        budget=HostBudget(ceiling_gib=ceiling_gib),
        model_footprint=_fixed_footprint(footprint_gib),
    )
    return manager, llm


def _seed_store(tmp_path: Path) -> RoleSelectionStore:
    store = RoleSelectionStore(tmp_path / "llm_selection.json")
    # All four roles start on claude_cli so no role holds a local model yet —
    # each test then assigns local models explicitly.
    store.write(
        RoleSelection(
            roles={
                role: LLMSelection(provider="claude_cli", lm_model=None, context_length={})
                for role in ROLES
            }
        )
    )
    return store


def _switcher(
    tmp_path: Path, manager: LMStudioManager
) -> tuple[RoleLLMSwitcher, RoleSelectionStore]:
    store = _seed_store(tmp_path)
    registry = RoleClientRegistry({role: FakeLLMClient() for role in ROLES})
    manager_registry = RoleManagerRegistry({"localhost:1234": manager})
    switcher = RoleLLMSwitcher(
        settings=_settings(),
        selection_store=store,
        registry=registry,
        manager_registry=manager_registry,
    )
    return switcher, store


def _lm(model: str) -> LLMSelection:
    return LLMSelection(
        provider="lm_studio",
        lm_model=model,
        context_length={},
        base_url="http://localhost:1234/v1",
    )


@pytest.mark.asyncio
async def test_two_roles_two_models_both_resident(tmp_path: Path) -> None:
    manager, llm = _local_manager(ceiling_gib=100.0)
    switcher, store = _switcher(tmp_path, manager)

    await switcher.swap_role("jarvis", _lm("modelA"))
    await switcher.swap_role("thinker", _lm("modelB"))

    # Both models resident on the host manager (true concurrency).
    assert manager.resident_model_ids() == frozenset({"modelA", "modelB"})
    assert manager.model_for_role("jarvis") == "modelA"
    assert manager.model_for_role("thinker") == "modelB"
    assert llm.unloaded == []  # multi-load — nothing evicted
    # Both selections persisted.
    persisted = store.read()
    assert persisted is not None
    assert persisted.role("jarvis").lm_model == "modelA"
    assert persisted.role("thinker").lm_model == "modelB"


@pytest.mark.asyncio
async def test_third_model_over_budget_refused_keeps_previous(tmp_path: Path) -> None:
    manager, llm = _local_manager(ceiling_gib=10.0, footprint_gib=4.0)
    switcher, store = _switcher(tmp_path, manager)

    await switcher.swap_role("jarvis", _lm("modelA"))
    await switcher.swap_role("thinker", _lm("modelB"))
    loaded_before = list(llm.loaded)

    # The third role (4+4+4 = 12 > 10) is refused BEFORE any load / rebuild.
    with pytest.raises(ModelBudgetExceededError):
        await switcher.swap_role("draft", _lm("modelC"))

    # Previous state stands: modelC not resident, draft unchanged in the store.
    assert "modelC" not in manager.resident_model_ids()
    assert manager.resident_model_ids() == frozenset({"modelA", "modelB"})
    assert llm.loaded == loaded_before
    persisted = store.read()
    assert persisted is not None
    assert persisted.role("draft").provider == "claude_cli"  # never flipped


@pytest.mark.asyncio
async def test_reselecting_resident_model_for_role_does_not_evict(tmp_path: Path) -> None:
    manager, llm = _local_manager(ceiling_gib=100.0)
    switcher, _store = _switcher(tmp_path, manager)

    await switcher.swap_role("jarvis", _lm("modelA"))
    await switcher.swap_role("thinker", _lm("modelB"))
    await switcher.swap_role("draft", _lm("modelA"))  # already resident

    assert manager.ref_count("modelA") == 2
    assert manager.resident_model_ids() == frozenset({"modelA", "modelB"})
    assert llm.unloaded == []


def test_put_role_over_budget_maps_to_409(tmp_path: Path) -> None:
    """The route surfaces a budget refusal as 409 with the plafond message."""

    manager, _llm = _local_manager(ceiling_gib=10.0, footprint_gib=4.0)
    store = _seed_store(tmp_path)
    registry = RoleClientRegistry({role: FakeLLMClient() for role in ROLES})
    manager_registry = RoleManagerRegistry({"localhost:1234": manager})
    switcher = RoleLLMSwitcher(
        settings=_settings(),
        selection_store=store,
        registry=registry,
        manager_registry=manager_registry,
    )
    # Pre-fill two roles so the third PUT trips the ceiling.
    manager.assign_role("jarvis", "modelA")
    manager.assign_role("thinker", "modelB")

    llm_router.set_role_switcher(switcher)
    llm_router.set_role_store_provider(lambda: store)
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.put(
            "/api/llm/roles/draft",
            json={
                "provider": "lm_studio",
                "lm_model": "modelC",
                "base_url": "http://localhost:1234/v1",
                "context_length": {},
            },
        )
    finally:
        llm_router.set_role_switcher(None)
        llm_router.reset_role_store_provider()
        llm_router.reset_settings_provider()

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "budget_exceeded"
    assert "plafond" in body["detail"]


def test_put_role_remote_host_unreachable_maps_to_503(tmp_path: Path) -> None:
    """A remote host that cannot be reached during the load policy → 503."""

    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    # No budget (remote, ceiling skipped) — the failure is the unreachable host.
    manager = LMStudioManager(
        host="studio.lan:1234",
        client_factory=_boom,
        budget=None,
        model_footprint=_fixed_footprint(4.0),
    )
    store = _seed_store(tmp_path)
    registry = RoleClientRegistry({role: FakeLLMClient() for role in ROLES})
    manager_registry = RoleManagerRegistry({"studio.lan:1234": manager})
    switcher = RoleLLMSwitcher(
        settings=_settings(),
        selection_store=store,
        registry=registry,
        manager_registry=manager_registry,
    )

    llm_router.set_role_switcher(switcher)
    llm_router.set_role_store_provider(lambda: store)
    llm_router.set_settings_provider(lambda: _settings())
    try:
        client = TestClient(app)
        response = client.put(
            "/api/llm/roles/jarvis",
            json={
                "provider": "lm_studio",
                "lm_model": "modelA",
                "base_url": "http://studio.lan:1234/v1",
                "context_length": {},
            },
        )
    finally:
        llm_router.set_role_switcher(None)
        llm_router.reset_role_store_provider()
        llm_router.reset_settings_provider()

    assert response.status_code == 503
    assert response.json()["error"] == "lm_studio_unavailable"
