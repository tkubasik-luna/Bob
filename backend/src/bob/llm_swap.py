"""Live LM Studio model swap coordinator (PRD 0012 / issue 0080).

This is a deep module owning the *validate-then-swap* dance behind a single
public method, :meth:`LLMSwitcher.swap_lm_model`. The REST route
(``PUT /api/llm/selection`` in :mod:`bob.llm_router`) is a thin shell that
delegates here.

The swap is:

1. Load the target model via :class:`bob.lm_studio_manager.LMStudioManager`
   (loads the new model at its default context length, unloads the previously
   loaded model). A load failure (OOM / unknown id / server down) aborts here
   — the previous selection is kept and the JSON is NOT written.
2. Rebuild the :class:`bob.llm_client.LLMClient` for BOTH orchestrator roles
   from the NEW selection, via :mod:`bob.llm.factory`.
3. Swap the reference held by the Orchestrator (``set_jarvis_client``) AND the
   sub-agent client holder (:class:`SubAgentClientHolder`).
4. Persist the new selection JSON.

Non-interruptive: both consumers read their client reference per request /
per task, so an in-flight turn finishes on the previous object and the next
request picks up the replacement (no cancellation, no draining). The whole
sequence is guarded by an :class:`asyncio.Lock` so concurrent ``PUT``s are
serialised — the second waits for the first to finish, then runs against the
already-updated state.

The SDK load call is blocking; it runs in a worker thread
(:func:`asyncio.to_thread`) so the event loop is not parked for the (generous)
load timeout.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass

from bob.config import Settings
from bob.llm.factory import build_jarvis_client, build_subagent_client
from bob.llm_client import LLMClient
from bob.llm_selection_store import LLMSelection, LLMSelectionStore
from bob.lm_studio_manager import LMStudioManager
from bob.orchestrator import Orchestrator

#: Providers the live switch (issue 0081) accepts. The selection model uses the
#: same two values; anything else is rejected before any probe runs.
_VALID_PROVIDERS = frozenset({"lm_studio", "claude_cli"})


class ClaudeCliUnavailableError(RuntimeError):
    """The ``claude`` CLI binary could not be found on ``PATH``.

    Raised by :meth:`LLMSwitcher.swap_provider` when validating a switch to the
    Claude CLI provider and ``shutil.which(settings.CLAUDE_CLI_BIN)`` returns
    ``None``. Distinct + catchable so the REST layer maps it to a clear HTTP
    error and the swap path keeps the previous provider (nothing written).
    """


class UnknownProviderError(ValueError):
    """The requested provider id is not one the live switch supports.

    Raised by :meth:`LLMSwitcher.swap_provider` for any value outside
    ``{"lm_studio", "claude_cli"}`` — a client bug or hand-crafted request. The
    route maps it to a 422-flavoured error; no probe, no swap, no write.
    """


class SubAgentClientHolder:
    """Mutable holder for the sub-agent :class:`LLMClient` reference.

    The sub-agent runner is rebuilt PER TASK by the runner factory in
    :mod:`bob.main`, which historically closed over a single client built once
    at boot. To swap the sub-agent client live (issue 0080) the factory reads
    from this holder instead of a frozen local, so a task spawned AFTER a swap
    gets the new client while any task already running keeps the old one it
    captured at construction. Trivial wrapper; the indirection is the point.
    """

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @property
    def client(self) -> LLMClient:
        """The current sub-agent client (read per task by the runner factory)."""

        return self._client

    def set(self, client: LLMClient) -> None:
        """Replace the held client reference (called by the swap coordinator)."""

        self._client = client


@dataclass(frozen=True)
class SwapResult:
    """Outcome of a successful :meth:`LLMSwitcher.swap_lm_model`."""

    selection: LLMSelection


class LLMSwitcher:
    """Serialised coordinator for the live LM Studio model swap.

    Owns the :class:`asyncio.Lock` that serialises concurrent swaps and the
    references it must update: the orchestrator (Jarvis role) and the sub-agent
    client holder. The selection store and LM Studio manager are injected so
    tests can drive the whole path against fakes.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        manager: LMStudioManager,
        selection_store: LLMSelectionStore,
        orchestrator: Orchestrator,
        subagent_holder: SubAgentClientHolder,
    ) -> None:
        self._settings = settings
        self._manager = manager
        self._selection_store = selection_store
        self._orchestrator = orchestrator
        self._subagent_holder = subagent_holder
        self._lock = asyncio.Lock()

    async def swap_lm_model(self, model_id: str) -> SwapResult:
        """Validate-then-swap to ``model_id`` (LM Studio provider).

        Blocking + serialised. On success: the target is loaded (default ctx),
        the previous model unloaded, both role clients rebuilt + swapped, and
        the new selection persisted. The returned :class:`SwapResult` carries
        the persisted selection.

        On failure the SDK error propagates unchanged
        (:class:`~bob.lm_studio_manager.LMStudioModelNotFoundError` /
        :class:`~bob.lm_studio_manager.LMStudioLoadError` /
        :class:`~bob.lm_studio_manager.LMStudioUnavailableError`); the previous
        selection is kept and nothing is written. The route maps the error type
        to an HTTP status.
        """

        async with self._lock:
            current = self._selection_store.read()
            context_length = self._resolve_context_length(current, model_id)

            # 1. Load the target (+ unload previous). Blocking SDK call → thread.
            #    A raised error exits the lock with NO state mutation.
            await asyncio.to_thread(self._manager.load, model_id, context_length)

            # 2. Build the next selection (preserve provider + the ctx map).
            new_selection = self._next_selection(current, model_id)

            # 3. Rebuild + swap both role clients from the NEW selection.
            self._orchestrator.set_jarvis_client(
                build_jarvis_client(self._settings, new_selection)
            )
            self._subagent_holder.set(build_subagent_client(self._settings, new_selection))

            # 4. Persist only after a fully successful swap.
            self._selection_store.write(new_selection)
            return SwapResult(selection=new_selection)

    async def swap_provider(self, provider: str) -> SwapResult:
        """Validate-then-swap the active provider (Claude CLI ↔ LM Studio).

        Mirrors :meth:`swap_lm_model`: lock-guarded, non-interruptive (both
        consumers read their client reference per request / per task), and
        validate-the-TARGET-before-mutating-anything:

        - ``lm_studio`` target → probe the LM Studio server is reachable
          (:meth:`LMStudioManager.list_models`, a no-load management call). An
          unreachable server raises :class:`~bob.lm_studio_manager.LMStudioUnavailableError`.
        - ``claude_cli`` target → verify the ``claude`` binary is on ``PATH``
          via ``shutil.which(settings.CLAUDE_CLI_BIN)``; absence raises
          :class:`ClaudeCliUnavailableError`.

        On validation success: build the next selection (provider changed, the
        per-model ctx map kept), rebuild BOTH role clients from it via the
        factory (which dispatches the backend off the new provider), swap the
        orchestrator + sub-agent holder references, then persist the JSON.

        On validation failure NOTHING is mutated — previous provider kept, no
        rebuild, no write — and the error propagates for the route to map. A
        no-op (provider already active) is still validated then rewritten so the
        contract ("the returned selection is the active one") holds uniformly.
        """

        if provider not in _VALID_PROVIDERS:
            raise UnknownProviderError(f"Unknown LLM provider: {provider!r}")

        async with self._lock:
            current = self._selection_store.read()

            # 1. Validate the TARGET before touching any state. A raised error
            #    exits the lock with no rebuild and no write.
            if provider == "lm_studio":
                # Reachability probe — a management list call (no model load).
                # Blocking SDK call → worker thread so the loop is not parked.
                await asyncio.to_thread(self._manager.list_models)
            else:  # claude_cli
                if shutil.which(self._settings.CLAUDE_CLI_BIN) is None:
                    raise ClaudeCliUnavailableError(
                        f"Claude CLI binary not found on PATH: "
                        f"{self._settings.CLAUDE_CLI_BIN!r}"
                    )

            # 2. Build the next selection (provider changed, ctx map + pinned
            #    LM model preserved so a later switch back is cheap).
            new_selection = self._next_provider_selection(current, provider)

            # 3. Rebuild + swap both role clients from the NEW selection. The
            #    factory dispatches the backend off ``new_selection.provider``.
            self._orchestrator.set_jarvis_client(
                build_jarvis_client(self._settings, new_selection)
            )
            self._subagent_holder.set(build_subagent_client(self._settings, new_selection))

            # 4. Persist only after a fully successful swap.
            self._selection_store.write(new_selection)
            return SwapResult(selection=new_selection)

    @staticmethod
    def _next_provider_selection(current: LLMSelection | None, provider: str) -> LLMSelection:
        """Build the selection to persist after a successful provider switch.

        Only ``provider`` changes; the pinned ``lm_model`` and the per-model
        context-length map round-trip unchanged so switching back to LM Studio
        restores the prior model without a re-pick.
        """

        lm_model = current.lm_model if current is not None else None
        context_length = dict(current.context_length) if current is not None else {}
        return LLMSelection(
            provider=provider,
            lm_model=lm_model,
            context_length=context_length,
        )

    @staticmethod
    def _resolve_context_length(current: LLMSelection | None, model_id: str) -> int | None:
        """Default context length for ``model_id`` from the persisted ctx map.

        Issue 0080 loads at the *default* context length: we pass the persisted
        per-model value when one round-tripped from a prior selection, else
        ``None`` so the SDK applies the model's own default. The ctx SLIDER
        (explicit override) is issue 0082, out of scope here.
        """

        if current is None:
            return None
        return current.context_length.get(model_id)

    @staticmethod
    def _next_selection(current: LLMSelection | None, model_id: str) -> LLMSelection:
        """Build the selection to persist after a successful load.

        Keeps the active provider (``lm_studio`` here) and the existing
        per-model context-length map; only ``lm_model`` changes.
        """

        provider = current.provider if current is not None else "lm_studio"
        context_length = dict(current.context_length) if current is not None else {}
        return LLMSelection(
            provider=provider,
            lm_model=model_id,
            context_length=context_length,
        )


def resolve_cold_start_model(
    selection: LLMSelection,
    manager: LMStudioManager,
) -> str | None:
    """Resolve the model to pin on a cold start (issue 0080).

    Cold start = provider is LM Studio and NO model is pinned anywhere
    (``selection.lm_model`` is falsy). Resolution order:

    1. A model already loaded in LM Studio (the first one), so a running server
       is respected and no extra load happens.
    2. Else the first chat-capable downloaded model.
    3. Else ``None`` (nothing downloaded / server unreachable) — the caller
       leaves the selection unpinned and the picker shows ``—``.

    A best-effort probe: an unreachable server collapses to ``None`` rather
    than crashing boot (the selection is a runtime preference, never load-
    bearing for the call path).
    """

    if selection.provider != "lm_studio" or selection.lm_model:
        return None
    try:
        loaded = manager.loaded_model_ids()
        if loaded:
            return loaded[0]
        models = manager.list_models()
    except Exception:  # boot probe must never crash startup
        return None
    return models[0].id if models else None
