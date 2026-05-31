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
from bob.llm_selection_store import LLMSelection, LLMSelectionStore
from bob.llm_swap import (
    LLMSwitcher,
    SubAgentClientHolder,
    resolve_cold_start_model,
)
from bob.lm_studio_manager import (
    LMStudioLoadError,
    LMStudioModel,
    LMStudioModelNotFoundError,
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
        self._loaded_ids: list[str] = []
        self._models: list[LMStudioModel] = []

    def load(self, model_id: str, context_length: int | None = None) -> None:
        if self.load_error is not None:
            raise self.load_error
        self.loads.append((model_id, context_length))

    def loaded_model_ids(self) -> list[str]:
        return list(self._loaded_ids)

    def list_models(self) -> list[LMStudioModel]:
        return list(self._models)


class _OrchestratorSpy:
    """Captures the Jarvis client the switcher pushes."""

    def __init__(self, client: LLMClient) -> None:
        self.jarvis_client = client

    def set_jarvis_client(self, client: LLMClient) -> None:
        self.jarvis_client = client


def _switcher(
    tmp_path: Path,
    *,
    manager: _FakeManager,
    initial: LLMSelection,
) -> tuple[LLMSwitcher, _OrchestratorSpy, SubAgentClientHolder, LLMSelectionStore]:
    store = LLMSelectionStore(tmp_path / "llm_selection.json")
    store.write(initial)
    orch = _OrchestratorSpy(FakeLLMClient())
    holder = SubAgentClientHolder(FakeLLMClient())
    switcher = LLMSwitcher(
        settings=_settings(),
        manager=cast(Any, manager),
        selection_store=store,
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
        def load(self, model_id: str, context_length: int | None = None) -> None:
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
