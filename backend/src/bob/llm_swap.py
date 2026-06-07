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
from collections.abc import Callable
from dataclasses import dataclass

from bob.config import Settings
from bob.context.policy import (
    DEFAULT_TOKEN_BUDGET,
    token_budget_for_context_length,
)
from bob.llm.factory import build_jarvis_client, build_role_client, build_subagent_client
from bob.llm_client import LLMClient
from bob.llm_selection_store import (
    ROLES,
    LLMSelection,
    LLMSelectionStore,
    RoleSelection,
    RoleSelectionStore,
)
from bob.lm_studio_manager import LMStudioManager, host_from_base_url
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

    async def swap_lm_model(self, model_id: str, context_length: int | None = None) -> SwapResult:
        """Validate-then-swap to ``model_id`` (LM Studio provider).

        Blocking + serialised. On success: the target is loaded, the previous
        model unloaded, both role clients rebuilt + swapped, the bounded-context
        token budget recomputed from the loaded ctx, and the new selection
        persisted. The returned :class:`SwapResult` carries the persisted
        selection.

        ``context_length`` (issue 0082) is the explicit ctx-slider Apply value:

        - when given, the target is loaded AT that window, the value is stored
          in the per-model ctx map, and the budget is coupled to it;
        - when ``None``, the persisted per-model ctx (if any) is reused, else the
          SDK applies the model's own default and the budget keeps
          :data:`~bob.context.policy.DEFAULT_TOKEN_BUDGET`.

        On failure the SDK error propagates unchanged
        (:class:`~bob.lm_studio_manager.LMStudioModelNotFoundError` /
        :class:`~bob.lm_studio_manager.LMStudioLoadError` /
        :class:`~bob.lm_studio_manager.LMStudioUnavailableError`); the previous
        selection is kept and nothing is written. The route maps the error type
        to an HTTP status.
        """

        async with self._lock:
            current = self._selection_store.read()
            effective_ctx = (
                context_length
                if context_length is not None
                else self._resolve_context_length(current, model_id)
            )

            # 1. Load the target (offload others first). Blocking SDK call →
            #    thread. A raised error exits the lock with NO state mutation.
            #    ``reload`` is forced only on an EXPLICIT ctx Apply (issue 0082):
            #    a plain re-select of an already-resident model is a no-op load
            #    (kept resident, just re-pinned) so we don't needlessly offload
            #    + reload a model that is already loaded.
            await asyncio.to_thread(
                self._manager.load,
                model_id,
                effective_ctx,
                reload=context_length is not None,
            )

            # 2. Build the next selection (preserve provider + the ctx map, and
            #    pin an explicit ctx override for this model when supplied).
            new_selection = self._next_selection(current, model_id, context_length)

            # 3. Rebuild + swap both role clients from the NEW selection.
            self._orchestrator.set_jarvis_client(build_jarvis_client(self._settings, new_selection))
            self._subagent_holder.set(build_subagent_client(self._settings, new_selection))

            # 4. Couple the bounded-context budget to the loaded ctx window.
            self._orchestrator.set_token_budget(token_budget_for_context_length(effective_ctx))

            # 5. Persist only after a fully successful swap.
            self._selection_store.write(new_selection)
            return SwapResult(selection=new_selection)

    async def swap_provider(self, provider: str) -> SwapResult:
        """Validate-then-swap the active provider (Claude CLI ↔ LM Studio).

        Mirrors :meth:`swap_lm_model`: lock-guarded, non-interruptive (both
        consumers read their client reference per request / per task), and
        validate-the-TARGET-before-mutating-anything:

        - ``lm_studio`` target → NO reachability gate. User must be able to
          switch to LM Studio even when the currently-configured server is
          offline (so they can then re-point ``base_url`` to a live one).
          Reachability surfaces in the UI's ping chip post-swap.
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

            # 1. Validate the TARGET. lm_studio: no gate (user must be able
            #    to switch even when the current server is offline). claude_cli:
            #    binary must exist on PATH.
            if provider == "claude_cli" and shutil.which(self._settings.CLAUDE_CLI_BIN) is None:
                raise ClaudeCliUnavailableError(
                    f"Claude CLI binary not found on PATH: {self._settings.CLAUDE_CLI_BIN!r}"
                )

            # 2. Build the next selection (provider changed, ctx map + pinned
            #    LM model preserved so a later switch back is cheap).
            new_selection = self._next_provider_selection(current, provider)

            # 3. Rebuild + swap both role clients from the NEW selection. The
            #    factory dispatches the backend off ``new_selection.provider``.
            self._orchestrator.set_jarvis_client(build_jarvis_client(self._settings, new_selection))
            self._subagent_holder.set(build_subagent_client(self._settings, new_selection))

            # 4. Recouple the bounded-context budget. Claude CLI has no ctx
            #    control → reset to the conservative default; LM Studio couples
            #    to the pinned model's persisted ctx (None → default too).
            if provider == "lm_studio":
                pinned_ctx = (
                    new_selection.context_length.get(new_selection.lm_model)
                    if new_selection.lm_model is not None
                    else None
                )
                self._orchestrator.set_token_budget(token_budget_for_context_length(pinned_ctx))
            else:
                self._orchestrator.set_token_budget(DEFAULT_TOKEN_BUDGET)

            # 5. Persist only after a fully successful swap.
            self._selection_store.write(new_selection)
            return SwapResult(selection=new_selection)

    async def swap_base_url(self, base_url: str) -> SwapResult:
        """Swap the LM Studio inference/management base URL — no reachability gate.

        Lock-guarded. The new URL drives BOTH the ``openai`` inference client
        (via the factory's ``LLM_BASE_URL`` override) and the management SDK
        host (derived ``host:port``).

        The target is NOT probed: the user must be able to re-point the server
        even when the current one is dead (and the next one not yet up). The
        UI's ping chip reflects live reachability post-swap.

        Steps: derive host → repoint manager → build next selection → rebuild
        both role clients → persist. Token budget left untouched.
        """

        url = base_url.strip()
        if not url:
            raise UnknownProviderError("base_url must be a non-empty string")

        async with self._lock:
            current = self._selection_store.read()
            new_host = host_from_base_url(url)

            self._manager.set_host(new_host)
            new_selection = self._next_base_url_selection(current, url)

            self._orchestrator.set_jarvis_client(build_jarvis_client(self._settings, new_selection))
            self._subagent_holder.set(build_subagent_client(self._settings, new_selection))

            self._selection_store.write(new_selection)
            return SwapResult(selection=new_selection)

    @staticmethod
    def _next_base_url_selection(current: LLMSelection | None, base_url: str) -> LLMSelection:
        """Build the selection to persist after a successful base-URL swap.

        Only ``base_url`` changes; provider / pinned model / ctx map round-trip
        unchanged.
        """

        return LLMSelection(
            provider=current.provider if current is not None else "lm_studio",
            lm_model=current.lm_model if current is not None else None,
            context_length=dict(current.context_length) if current is not None else {},
            base_url=base_url,
        )

    @staticmethod
    def _next_provider_selection(current: LLMSelection | None, provider: str) -> LLMSelection:
        """Build the selection to persist after a successful provider switch.

        Only ``provider`` changes; the pinned ``lm_model`` and the per-model
        context-length map round-trip unchanged so switching back to LM Studio
        restores the prior model without a re-pick.
        """

        lm_model = current.lm_model if current is not None else None
        context_length = dict(current.context_length) if current is not None else {}
        base_url = current.base_url if current is not None else None
        return LLMSelection(
            provider=provider,
            lm_model=lm_model,
            context_length=context_length,
            base_url=base_url,
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
    def _next_selection(
        current: LLMSelection | None,
        model_id: str,
        context_length: int | None = None,
    ) -> LLMSelection:
        """Build the selection to persist after a successful load.

        Keeps the active provider (``lm_studio`` here) and the existing
        per-model context-length map; only ``lm_model`` changes. When an
        explicit ``context_length`` is supplied (issue 0082 ctx-slider Apply) it
        is pinned for this model in the map so returning to the model reapplies
        it; ``None`` leaves any prior per-model value untouched.
        """

        provider = current.provider if current is not None else "lm_studio"
        ctx_map = dict(current.context_length) if current is not None else {}
        base_url = current.base_url if current is not None else None
        if context_length is not None:
            ctx_map[model_id] = context_length
        return LLMSelection(
            provider=provider,
            lm_model=model_id,
            context_length=ctx_map,
            base_url=base_url,
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


# =============================================================================
# Per-role swap coordinator — PRD 0016 / issue 0106
# =============================================================================
#
# Where :class:`LLMSwitcher` rebuilds BOTH role clients on every swap (the
# pre-0016 single-selection world), :class:`RoleLLMSwitcher` rebuilds ONLY the
# changed role's client and leaves the other three untouched. It owns:
#
# - a :class:`RoleClientRegistry` (the current client per role), and
# - a per-role *sink* callback (where a rebuilt client is pushed: the
#   orchestrator's ``set_jarvis_client`` for ``jarvis``, the sub-agent holder's
#   ``set`` for ``subagent``; ``thinker`` / ``draft`` are self-held in the
#   registry until the slices that consume them wire their own sinks).
#
# It deliberately does NOT touch the LM Studio manager's load/offload policy —
# multi-load + budget is a later slice (S11, Annexe J). A model swap that needs
# a load still goes through :class:`LLMSwitcher`; this coordinator owns the
# *selection + client rebuild* half of the per-role contract.


class RoleClientRegistry:
    """Mutable per-role :class:`LLMClient` registry with optional sinks.

    For each role the registry holds the current client and an optional *sink*
    — a setter invoked when the role's client is rebuilt so a downstream
    consumer (the orchestrator, the sub-agent holder) picks up the replacement.
    Roles with no sink are simply held here (read by a future consumer).

    The indirection is the point: swapping one role replaces exactly one entry
    and fires exactly one sink, so the other roles' client OBJECTS are unchanged
    (the read-per-request consumers therefore never see a foreign-role swap).
    """

    def __init__(
        self,
        clients: dict[str, LLMClient],
        *,
        sinks: dict[str, Callable[[LLMClient], None]] | None = None,
    ) -> None:
        self._clients: dict[str, LLMClient] = dict(clients)
        self._sinks: dict[str, Callable[[LLMClient], None]] = dict(sinks or {})

    def get(self, role: str) -> LLMClient:
        """Return the current client for ``role`` (KeyError if unknown)."""

        return self._clients[role]

    def set(self, role: str, client: LLMClient) -> None:
        """Replace ``role``'s client and fire its sink (if any)."""

        if role not in ROLES:
            raise KeyError(f"Unknown LLM role: {role!r}")
        self._clients[role] = client
        sink = self._sinks.get(role)
        if sink is not None:
            sink(client)


class RoleLLMSwitcher:
    """Serialised coordinator for per-role selection swaps (rebuild ONE role).

    Owns the :class:`asyncio.Lock` serialising concurrent per-role swaps, the
    :class:`RoleSelectionStore`, and the :class:`RoleClientRegistry`. A
    :meth:`swap_role` call mutates exactly one role of the persisted map,
    rebuilds ONLY that role's client, pushes it through the registry (firing the
    role's sink), and persists the new map. The other three roles' selections
    and client objects round-trip untouched.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        selection_store: RoleSelectionStore,
        registry: RoleClientRegistry,
    ) -> None:
        self._settings = settings
        self._selection_store = selection_store
        self._registry = registry
        self._lock = asyncio.Lock()

    async def swap_role(self, role: str, selection: LLMSelection) -> RoleSelection:
        """Replace ``role``'s selection with ``selection``; rebuild only that role.

        Lock-guarded. Reads the current map (seeding from defaults when the file
        is somehow absent), swaps in the new per-role :class:`LLMSelection`,
        rebuilds ONLY that role's client via
        :func:`bob.llm.factory.build_role_client`, pushes it through the
        registry, and persists the v2 map. Returns the full persisted
        :class:`RoleSelection`.

        Raises :class:`UnknownProviderError` for an unknown role or an invalid
        provider, BEFORE any rebuild / write, so a bad request keeps the
        previous state.
        """

        if role not in ROLES:
            raise UnknownProviderError(f"Unknown LLM role: {role!r}")
        if selection.provider not in _VALID_PROVIDERS:
            raise UnknownProviderError(f"Unknown LLM provider: {selection.provider!r}")

        async with self._lock:
            current = self._selection_store.read()
            if current is None:
                current = _seed_role_selection_for_swap(self._settings)

            next_map = current.with_role(role, selection)

            # Rebuild ONLY the changed role's client; the others are untouched.
            rebuilt = build_role_client(next_map, role, self._settings)
            self._registry.set(role, rebuilt)

            self._selection_store.write(next_map)
            return next_map


def _seed_role_selection_for_swap(settings: Settings) -> RoleSelection:
    """Fallback role map when the per-role store is unexpectedly empty.

    The boot path always seeds the store, so this is a belt-and-suspenders path
    (e.g. a test that swaps before seeding): build a flat ``.env`` selection and
    fan it across the four roles, identical to the store's own first-boot seed.
    """

    flat = LLMSelection(
        provider=settings.LLM_PROVIDER,
        lm_model=settings.LLM_MODEL,
        context_length={},
        base_url=settings.LLM_BASE_URL or None,
    )
    return RoleSelection(roles={role: flat for role in ROLES})
