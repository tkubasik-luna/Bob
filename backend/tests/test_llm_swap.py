"""Tests for the live LM Studio model swap coordinator (PRD 0012 / issue 0080).

The ``lmstudio`` SDK is never touched here: the swap coordinator depends on
:class:`bob.lm_studio_manager.LMStudioManager` through a fake that records its
``load`` calls (offline + deterministic). We assert the validate-then-swap
contract directly:

- success → target loaded, both role clients rebuilt + swapped, JSON written;
- load failure → previous clients kept, JSON NOT written, error propagates;
- in-flight reference → a coroutine that captured the old client keeps it;
- cold-start resolution (loaded-first, else first downloaded, else None);
- the ``asyncio.Lock`` serialises concurrent swaps.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from bob.config import Settings
from bob.llm_client import LLMClient
from bob.llm_selection_store import ROLES, LLMSelection, RoleSelection, RoleSelectionStore
from bob.llm_swap import (
    ClaudeCliUnavailableError,
    LLMSwitcher,
    SubAgentClientHolder,
    UnknownProviderError,
    resolve_cold_start_model,
)
from bob.lm_studio_manager import (
    LMStudioLoadError,
    LMStudioModel,
    LMStudioModelNotFoundError,
    LMStudioUnavailableError,
)
from bob.orchestrator import Orchestrator

from ._harness.fake_llm import FakeLLMClient


def _settings() -> Settings:
    return Settings(
        LLM_PROVIDER="lm_studio",
        LLM_BASE_URL="http://localhost:1234/v1",
        LLM_MODEL="boot-model",
        LLM_API_KEY="not-needed",
    )


class _FakeManager:
    """Records ``load`` calls; optionally raises to model a load failure."""

    def __init__(self, load_error: Exception | None = None) -> None:
        self.load_error = load_error
        self.loads: list[tuple[str, int | None]] = []
        self.reloads: list[bool] = []
        self.host = "localhost:1234"
        self._loaded_ids: list[str] = []
        self._models: list[LMStudioModel] = []

    def load(
        self, model_id: str, context_length: int | None = None, *, reload: bool = False
    ) -> None:
        if self.load_error is not None:
            raise self.load_error
        self.loads.append((model_id, context_length))
        self.reloads.append(reload)

    def loaded_model_ids(self) -> list[str]:
        return list(self._loaded_ids)

    def list_models(self) -> list[LMStudioModel]:
        return list(self._models)

    def set_host(self, host: str) -> None:
        self.host = host


class _OrchestratorSpy:
    """Captures the Jarvis client + token budget the switcher pushes."""

    def __init__(self, client: LLMClient) -> None:
        self.jarvis_client = client
        self.token_budget: int | None = None

    def set_jarvis_client(self, client: LLMClient) -> None:
        self.jarvis_client = client

    def set_token_budget(self, token_budget: int) -> None:
        self.token_budget = token_budget


class _FlatStore:
    """Flat (jarvis-view) test adapter over the per-role store.

    The switcher takes the RoleSelectionStore directly; these tests assert the
    GLOBAL surface's contract, which is the jarvis projection — the adapter
    keeps the assertions in flat terms.
    """

    def __init__(self, path: Path) -> None:
        self.role_store = RoleSelectionStore(path)

    def write(self, selection: LLMSelection) -> None:
        current = self.role_store.read()
        if current is None:
            self.role_store.write(RoleSelection(roles={role: selection for role in ROLES}))
        else:
            self.role_store.write(
                current.with_role("jarvis", selection).with_role("subagent", selection)
            )

    def read(self) -> LLMSelection | None:
        role_selection = self.role_store.read()
        return role_selection.role("jarvis") if role_selection is not None else None


def _switcher(
    tmp_path: Path,
    *,
    manager: _FakeManager,
    initial: LLMSelection,
) -> tuple[LLMSwitcher, _OrchestratorSpy, SubAgentClientHolder, _FlatStore]:
    store = _FlatStore(tmp_path / "llm_selection.json")
    store.write(initial)
    orch = _OrchestratorSpy(FakeLLMClient())
    holder = SubAgentClientHolder(FakeLLMClient())
    switcher = LLMSwitcher(
        settings=_settings(),
        manager=cast(Any, manager),
        selection_store=store.role_store,
        orchestrator=cast(Orchestrator, orch),
        subagent_holder=holder,
    )
    return switcher, orch, holder, store


@pytest.mark.asyncio
async def test_swap_loads_rebuilds_both_clients_and_writes_json(tmp_path: Path) -> None:
    manager = _FakeManager()
    initial = LLMSelection(
        provider="lm_studio",
        lm_model="boot-model",
        context_length={"target-model": 16384},
    )
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    old_jarvis = orch.jarvis_client
    old_subagent = holder.client

    result = await switcher.swap_lm_model("target-model")

    # Loaded at the persisted (default) ctx for that model.
    assert manager.loads == [("target-model", 16384)]
    # Both role clients were rebuilt (new objects) and swapped.
    assert orch.jarvis_client is not old_jarvis
    assert holder.client is not old_subagent
    # The persisted selection reflects the new model id (provider + ctx map kept).
    assert result.selection.lm_model == "target-model"
    persisted = store.read()
    assert persisted is not None
    assert persisted.lm_model == "target-model"
    assert persisted.provider == "lm_studio"
    assert persisted.context_length == {"target-model": 16384}


class _AcloseSpyClient(FakeLLMClient):
    """A fake client exposing ``aclose`` — stands in for the SDK transport."""

    def __init__(self) -> None:
        super().__init__()
        self.aclose_count = 0

    async def aclose(self) -> None:
        self.aclose_count += 1


@pytest.mark.asyncio
async def test_swap_closes_superseded_sdk_clients(tmp_path: Path) -> None:
    """Both superseded role clients (SDK transport) are ``aclose``-d after swap."""

    manager = _FakeManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    store = _FlatStore(tmp_path / "llm_selection.json")
    store.write(initial)
    old_jarvis = _AcloseSpyClient()
    old_subagent = _AcloseSpyClient()
    orch = _OrchestratorSpy(old_jarvis)
    holder = SubAgentClientHolder(old_subagent)
    switcher = LLMSwitcher(
        settings=_settings(),
        manager=cast(Any, manager),
        selection_store=store.role_store,
        orchestrator=cast(Orchestrator, orch),
        subagent_holder=holder,
    )

    await switcher.swap_lm_model("target-model")

    # Both superseded clients were torn down exactly once; the replacements are
    # in place (and are NOT the spies).
    assert old_jarvis.aclose_count == 1
    assert old_subagent.aclose_count == 1
    assert orch.jarvis_client is not old_jarvis
    assert holder.client is not old_subagent


@pytest.mark.asyncio
async def test_swap_load_failure_keeps_previous_clients_and_does_not_write(
    tmp_path: Path,
) -> None:
    manager = _FakeManager(load_error=LMStudioLoadError("out of memory"))
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    old_jarvis = orch.jarvis_client
    old_subagent = holder.client

    with pytest.raises(LMStudioLoadError):
        await switcher.swap_lm_model("target-model")

    # Previous clients retained — no swap on a failed load.
    assert orch.jarvis_client is old_jarvis
    assert holder.client is old_subagent
    # JSON untouched: still the boot model.
    persisted = store.read()
    assert persisted is not None
    assert persisted.lm_model == "boot-model"


@pytest.mark.asyncio
async def test_swap_not_found_propagates(tmp_path: Path) -> None:
    manager = _FakeManager(load_error=LMStudioModelNotFoundError("ghost"))
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, _orch, _holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    with pytest.raises(LMStudioModelNotFoundError):
        await switcher.swap_lm_model("ghost-model")
    assert store.read().lm_model == "boot-model"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_in_flight_call_keeps_old_client(tmp_path: Path) -> None:
    """A coroutine that captured the previous client finishes on it.

    Mirrors the orchestrator/runner read-per-request pattern: the consumer
    binds ``orch.jarvis_client`` into a local *before* the swap, so the swap
    replacing the attribute does not affect the in-flight binding.
    """

    manager = _FakeManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, orch, _holder, _store = _switcher(tmp_path, manager=manager, initial=initial)

    captured = orch.jarvis_client  # an in-flight turn already bound this object

    await switcher.swap_lm_model("target-model")

    # The swap replaced the attribute, but the captured reference is unchanged.
    assert orch.jarvis_client is not captured
    assert captured is not orch.jarvis_client


@pytest.mark.asyncio
async def test_lock_serialises_concurrent_swaps(tmp_path: Path) -> None:
    """Two concurrent swaps run one-at-a-time under the asyncio.Lock.

    The fake manager's ``load`` asserts it is never re-entered while a prior
    load is mid-flight; both swaps complete and the LAST write wins.
    """

    import threading
    import time

    state = {"concurrent": 0, "max_concurrent": 0}
    guard = threading.Lock()

    class _SerialManager(_FakeManager):
        def load(
            self, model_id: str, context_length: int | None = None, *, reload: bool = False
        ) -> None:
            # Runs in a worker thread (via to_thread). If the asyncio.Lock
            # failed to serialise, the two ``load`` bodies would overlap here;
            # we hold the body open with a sleep so any overlap is observable.
            with guard:
                state["concurrent"] += 1
                state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
            time.sleep(0.02)
            with guard:
                state["concurrent"] -= 1
            self.loads.append((model_id, context_length))

    manager = _SerialManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, _orch, _holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    await asyncio.gather(
        switcher.swap_lm_model("model-a"),
        switcher.swap_lm_model("model-b"),
    )

    assert len(manager.loads) == 2
    # The lock kept the two loads strictly non-overlapping.
    assert state["max_concurrent"] == 1
    # Both ran; the persisted model is one of the two (last writer wins).
    final = store.read()
    assert final is not None
    assert final.lm_model in {"model-a", "model-b"}


# --- context-length override + budget coupling (issue 0082) ------------------


@pytest.mark.asyncio
async def test_swap_with_explicit_ctx_loads_at_ctx_persists_and_couples_budget(
    tmp_path: Path,
) -> None:
    """An explicit ctx Apply loads at that window, pins it per-model, couples budget."""

    manager = _FakeManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, orch, _holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    await switcher.swap_lm_model("target-model", 32768)

    # Loaded AT the explicit ctx (not the model default).
    assert manager.loads == [("target-model", 32768)]
    # Pinned per-model in the JSON for reapply on return.
    persisted = store.read()
    assert persisted is not None
    assert persisted.context_length == {"target-model": 32768}
    # Budget coupled: max(2048, 32768 - 6000) = 26768.
    assert orch.token_budget == 26768


@pytest.mark.asyncio
async def test_swap_without_ctx_reuses_persisted_per_model_ctx(tmp_path: Path) -> None:
    """No explicit ctx → the persisted per-model value drives load + budget."""

    manager = _FakeManager()
    initial = LLMSelection(
        provider="lm_studio",
        lm_model="boot-model",
        context_length={"target-model": 16384},
    )
    switcher, orch, _holder, _store = _switcher(tmp_path, manager=manager, initial=initial)

    await switcher.swap_lm_model("target-model")

    assert manager.loads == [("target-model", 16384)]
    # max(2048, 16384 - 6000) = 10384.
    assert orch.token_budget == 10384


@pytest.mark.asyncio
async def test_swap_without_any_ctx_keeps_default_budget(tmp_path: Path) -> None:
    """No explicit ctx and no persisted ctx → model default load, default budget."""

    manager = _FakeManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, orch, _holder, _store = _switcher(tmp_path, manager=manager, initial=initial)

    await switcher.swap_lm_model("target-model")

    assert manager.loads == [("target-model", None)]
    assert orch.token_budget == 2048  # DEFAULT_TOKEN_BUDGET


@pytest.mark.asyncio
async def test_swap_to_claude_resets_budget_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching to Claude CLI (no ctx control) resets the budget to the default."""

    monkeypatch.setattr("bob.llm_swap.shutil.which", lambda _bin: "/usr/local/bin/claude")
    manager = _FakeManager()
    initial = LLMSelection(
        provider="lm_studio",
        lm_model="boot-model",
        context_length={"boot-model": 32768},
    )
    switcher, orch, _holder, _store = _switcher(tmp_path, manager=manager, initial=initial)

    await switcher.swap_provider("claude_cli")

    assert orch.token_budget == 2048  # DEFAULT_TOKEN_BUDGET


@pytest.mark.asyncio
async def test_swap_to_lm_studio_couples_budget_to_pinned_ctx(tmp_path: Path) -> None:
    """Switching to LM Studio couples the budget to the pinned model's ctx."""

    manager = _FakeManager()
    initial = LLMSelection(
        provider="claude_cli",
        lm_model="boot-model",
        context_length={"boot-model": 16384},
    )
    switcher, orch, _holder, _store = _switcher(tmp_path, manager=manager, initial=initial)

    await switcher.swap_provider("lm_studio")

    # max(2048, 16384 - 6000) = 10384.
    assert orch.token_budget == 10384


# --- provider swap (issue 0081) ----------------------------------------------


@pytest.mark.asyncio
async def test_swap_provider_to_claude_when_binary_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude CLI target with the binary on PATH → both clients rebuilt + JSON written."""

    monkeypatch.setattr("bob.llm_swap.shutil.which", lambda _bin: "/usr/local/bin/claude")
    manager = _FakeManager()
    initial = LLMSelection(
        provider="lm_studio",
        lm_model="boot-model",
        context_length={"boot-model": 8192},
    )
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    old_jarvis = orch.jarvis_client
    old_subagent = holder.client

    result = await switcher.swap_provider("claude_cli")

    # No LM Studio load happens on a Claude-target switch (validation is which()).
    assert manager.loads == []
    # Both role clients were rebuilt (new objects) and swapped.
    assert orch.jarvis_client is not old_jarvis
    assert holder.client is not old_subagent
    # Persisted: provider flipped, pinned model + ctx map preserved.
    assert result.selection.provider == "claude_cli"
    persisted = store.read()
    assert persisted is not None
    assert persisted.provider == "claude_cli"
    assert persisted.lm_model == "boot-model"
    assert persisted.context_length == {"boot-model": 8192}


@pytest.mark.asyncio
async def test_swap_provider_to_claude_missing_binary_keeps_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude CLI target with NO binary on PATH → error, previous provider kept, no write."""

    monkeypatch.setattr("bob.llm_swap.shutil.which", lambda _bin: None)
    manager = _FakeManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    old_jarvis = orch.jarvis_client
    old_subagent = holder.client

    with pytest.raises(ClaudeCliUnavailableError):
        await switcher.swap_provider("claude_cli")

    # Nothing mutated — previous clients + persisted provider retained.
    assert orch.jarvis_client is old_jarvis
    assert holder.client is old_subagent
    persisted = store.read()
    assert persisted is not None
    assert persisted.provider == "lm_studio"


@pytest.mark.asyncio
async def test_swap_provider_to_lm_studio_when_reachable(tmp_path: Path) -> None:
    """LM Studio target with a reachable server → both clients rebuilt + JSON written."""

    manager = _FakeManager()  # list_models() returns [] → reachable
    initial = LLMSelection(provider="claude_cli", lm_model="boot-model", context_length={})
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    old_jarvis = orch.jarvis_client
    old_subagent = holder.client

    result = await switcher.swap_provider("lm_studio")

    assert orch.jarvis_client is not old_jarvis
    assert holder.client is not old_subagent
    assert result.selection.provider == "lm_studio"
    persisted = store.read()
    assert persisted is not None
    assert persisted.provider == "lm_studio"


@pytest.mark.asyncio
async def test_swap_provider_to_lm_studio_unreachable_still_swaps(tmp_path: Path) -> None:
    """No reachability gate — user must be able to switch to LM Studio even when offline."""

    class _UnreachableManager(_FakeManager):
        def list_models(self) -> list[LMStudioModel]:
            raise LMStudioUnavailableError("server down")

    manager = _UnreachableManager()
    initial = LLMSelection(provider="claude_cli", lm_model=None, context_length={})
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    old_jarvis = orch.jarvis_client
    old_subagent = holder.client

    result = await switcher.swap_provider("lm_studio")

    assert orch.jarvis_client is not old_jarvis
    assert holder.client is not old_subagent
    persisted = store.read()
    assert persisted is not None
    assert persisted.provider == "lm_studio"
    assert result.selection.provider == "lm_studio"


@pytest.mark.asyncio
async def test_swap_lm_model_sdk_unreachable_but_served_over_http_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK pre-load down + model served over OpenAI HTTP → pin + swap (JIT inference).

    For a remote / OpenAI-only server the lmstudio SDK websocket can be down even
    though the HTTP API serves the model. The SDK pre-load is an optimisation, so
    the swap proceeds (the openai client JIT-loads) instead of hard-failing.
    """

    manager = _FakeManager(load_error=LMStudioUnavailableError("sdk down"))
    initial = LLMSelection(
        provider="lm_studio",
        lm_model="boot-model",
        context_length={},
        base_url="http://192.168.4.94:1234/v1",
    )
    switcher, orch, _holder, store = _switcher(tmp_path, manager=manager, initial=initial)
    # The OpenAI endpoint confirms the model is served.
    monkeypatch.setattr("bob.llm_swap.model_served_over_http", lambda _url, _model: True)

    old_jarvis = orch.jarvis_client
    result = await switcher.swap_lm_model("target-model")

    assert orch.jarvis_client is not old_jarvis  # clients rebuilt despite no pre-load
    assert result.selection.lm_model == "target-model"
    assert store.read().lm_model == "target-model"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_swap_lm_model_sdk_unreachable_and_not_served_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK down AND the model isn't served over HTTP → genuinely unavailable, re-raise."""

    manager = _FakeManager(load_error=LMStudioUnavailableError("sdk down"))
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, orch, _holder, store = _switcher(tmp_path, manager=manager, initial=initial)
    monkeypatch.setattr("bob.llm_swap.model_served_over_http", lambda _url, _model: False)

    old_jarvis = orch.jarvis_client
    with pytest.raises(LMStudioUnavailableError):
        await switcher.swap_lm_model("target-model")

    # Nothing mutated — previous selection kept, client untouched.
    assert orch.jarvis_client is old_jarvis
    assert store.read().lm_model == "boot-model"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_swap_provider_unknown_value_rejected_before_any_probe(tmp_path: Path) -> None:
    """An unknown provider id raises before any probe / rebuild / write."""

    manager = _FakeManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, orch, _holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    old_jarvis = orch.jarvis_client

    with pytest.raises(UnknownProviderError):
        await switcher.swap_provider("gpt5")

    assert manager.loads == []
    assert orch.jarvis_client is old_jarvis
    assert store.read().provider == "lm_studio"  # type: ignore[union-attr]


# --- cold-start resolution ---------------------------------------------------


def test_cold_start_prefers_already_loaded_model() -> None:
    manager = _FakeManager()
    manager._loaded_ids = ["loaded-x", "loaded-y"]
    manager._models = [LMStudioModel("downloaded-z", None, None, None, False)]
    selection = LLMSelection(provider="lm_studio", lm_model=None, context_length={})

    assert resolve_cold_start_model(selection, cast(Any, manager)) == "loaded-x"


def test_cold_start_falls_back_to_first_downloaded() -> None:
    manager = _FakeManager()
    manager._loaded_ids = []
    manager._models = [
        LMStudioModel("first-downloaded", None, None, None, False),
        LMStudioModel("second", None, None, None, False),
    ]
    selection = LLMSelection(provider="lm_studio", lm_model=None, context_length={})

    assert resolve_cold_start_model(selection, cast(Any, manager)) == "first-downloaded"


def test_cold_start_none_when_nothing_available() -> None:
    manager = _FakeManager()
    selection = LLMSelection(provider="lm_studio", lm_model=None, context_length={})
    assert resolve_cold_start_model(selection, cast(Any, manager)) is None


def test_cold_start_skipped_when_model_already_pinned() -> None:
    manager = _FakeManager()
    manager._loaded_ids = ["loaded-x"]
    selection = LLMSelection(provider="lm_studio", lm_model="pinned", context_length={})
    assert resolve_cold_start_model(selection, cast(Any, manager)) is None


def test_cold_start_unreachable_server_returns_none() -> None:
    class _BoomManager(_FakeManager):
        def loaded_model_ids(self) -> list[str]:
            raise RuntimeError("server down")

    manager = _BoomManager()
    selection = LLMSelection(provider="lm_studio", lm_model=None, context_length={})
    assert resolve_cold_start_model(selection, cast(Any, manager)) is None


# --- base-URL swap (runtime LM Studio server reconfig) ----------------------


@pytest.mark.asyncio
async def test_swap_base_url_repoints_host_rebuilds_and_persists(tmp_path: Path) -> None:
    manager = _FakeManager()
    initial = LLMSelection(
        provider="lm_studio",
        lm_model="boot-model",
        context_length={},
        base_url="http://localhost:1234/v1",
    )
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)
    jarvis_before, sub_before = orch.jarvis_client, holder.client

    result = await switcher.swap_base_url("http://192.168.1.20:1234/v1")

    # Manager repointed to the derived host:port (scheme + /v1 stripped).
    assert manager.host == "192.168.1.20:1234"
    # Both role clients rebuilt + swapped.
    assert orch.jarvis_client is not jarvis_before
    assert holder.client is not sub_before
    # Persisted: only base_url changed; model + provider preserved.
    persisted = store.read()
    assert persisted is not None
    assert persisted.base_url == "http://192.168.1.20:1234/v1"
    assert persisted.lm_model == "boot-model"
    assert persisted.provider == "lm_studio"
    assert result.selection.base_url == "http://192.168.1.20:1234/v1"


@pytest.mark.asyncio
async def test_swap_base_url_unreachable_still_swaps(tmp_path: Path) -> None:
    """No reachability gate: user must be able to re-point a dead server."""

    class _UnreachableManager(_FakeManager):
        def list_models(self) -> list[LMStudioModel]:
            raise LMStudioUnavailableError("connection refused")

    manager = _UnreachableManager()
    manager.set_host("localhost:1234")
    initial = LLMSelection(
        provider="lm_studio",
        lm_model="boot-model",
        context_length={},
        base_url="http://localhost:1234/v1",
    )
    switcher, orch, holder, store = _switcher(tmp_path, manager=manager, initial=initial)
    jarvis_before, sub_before = orch.jarvis_client, holder.client

    result = await switcher.swap_base_url("http://10.0.0.9:9999/v1")

    # Host repointed; both clients rebuilt; selection persisted.
    assert manager.host == "10.0.0.9:9999"
    assert orch.jarvis_client is not jarvis_before
    assert holder.client is not sub_before
    persisted = store.read()
    assert persisted is not None
    assert persisted.base_url == "http://10.0.0.9:9999/v1"
    assert result.selection.base_url == "http://10.0.0.9:9999/v1"


@pytest.mark.asyncio
async def test_global_swap_preserves_other_roles_pins(tmp_path: Path) -> None:
    """Clobber regression: a GLOBAL swap must not flatten the per-role map.

    The global coordinator persists into jarvis + subagent only; a thinker /
    draft pinned via the per-role picker survives byte-for-byte. (The old flat
    v1 store rewrote the whole file flat and destroyed these pins.)
    """

    manager = _FakeManager()
    initial = LLMSelection(provider="lm_studio", lm_model="boot-model", context_length={})
    switcher, _orch, _holder, store = _switcher(tmp_path, manager=manager, initial=initial)

    # User pins thinker + draft to their own selections via the per-role picker.
    role_map = store.role_store.read()
    assert role_map is not None
    store.role_store.write(
        role_map.with_role(
            "thinker",
            LLMSelection(provider="lm_studio", lm_model="thinker-model", context_length={}),
        ).with_role(
            "draft",
            LLMSelection(provider="claude_cli", lm_model=None, context_length={}),
        )
    )

    await switcher.swap_lm_model("target-model")

    after = store.role_store.read()
    assert after is not None
    assert after.role("jarvis").lm_model == "target-model"
    assert after.role("subagent").lm_model == "target-model"
    assert after.role("thinker").lm_model == "thinker-model"
    assert after.role("draft").provider == "claude_cli"
