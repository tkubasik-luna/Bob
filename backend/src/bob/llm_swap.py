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
from dataclasses import dataclass, replace

from bob.config import Settings
from bob.context.policy import (
    DEFAULT_TOKEN_BUDGET,
    token_budget_for_context_length,
)
from bob.debug_log import emit_debug
from bob.llm.factory import build_jarvis_client, build_role_client, build_subagent_client
from bob.llm_client import LLMClient
from bob.llm_selection_store import (
    REASONING_LEVELS,
    ROLES,
    LLMSelection,
    RoleSelection,
    RoleSelectionStore,
)
from bob.lm_studio_manager import (
    LMStudioManager,
    LMStudioUnavailableError,
    host_from_base_url,
    model_served_over_http,
)
from bob.orchestrator import Orchestrator

#: Providers the live switch (issue 0081) accepts. The selection model uses the
#: same two values; anything else is rejected before any probe runs.
_VALID_PROVIDERS = frozenset({"lm_studio", "claude_cli"})


async def aclose_client(client: LLMClient | None) -> None:
    """Close a superseded client's async resource — a no-op for non-SDK clients.

    The LM Studio SDK transport (issue 0115) holds a long-lived websocket via
    :meth:`bob.llm.lmstudio_sdk.client.LMStudioSDKClient.aclose`; when a swap
    rebuilds a role's client the OLD one must be torn down or its socket leaks.
    The OpenAI :class:`bob.llm_client.LMStudioClient` and
    :class:`bob.llm_client.ClaudeCliClient` hold no async resource and expose no
    ``aclose`` — duck-typed here so closing them is a safe no-op (the swap path
    is identical for every transport). Failures are swallowed: a failed close of
    a superseded client must never abort a successful swap.
    """

    if client is None:
        return
    aclose = getattr(client, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # superseded client; a close failure is non-fatal
        emit_debug(
            category="llm",
            severity="warn",
            source="bob.llm_swap.aclose_client",
            summary="Failed to close superseded LLM client (ignored)",
            payload={"client_type": client.__class__.__name__},
        )


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

    The store is the per-role :class:`RoleSelectionStore` (the SINGLE owner of
    ``llm_selection.json``). This global coordinator reads the ``jarvis`` role
    as its flat view and persists a swap into ``jarvis`` + ``subagent`` —
    exactly the two clients it rebuilds — leaving thinker / draft / stt /
    budget untouched. (The old flat v1 store wrote the whole file flat and
    destroyed the per-role map on every global swap.)
    """

    def __init__(
        self,
        *,
        settings: Settings,
        manager: LMStudioManager,
        selection_store: RoleSelectionStore,
        orchestrator: Orchestrator,
        subagent_holder: SubAgentClientHolder,
    ) -> None:
        self._settings = settings
        self._manager = manager
        self._selection_store = selection_store
        self._orchestrator = orchestrator
        self._subagent_holder = subagent_holder
        self._lock = asyncio.Lock()

    def _read_flat(self) -> LLMSelection | None:
        """The global surface's flat view: the ``jarvis`` role's selection."""

        role_selection = self._selection_store.read()
        return role_selection.role("jarvis") if role_selection is not None else None

    def _write_flat(self, selection: LLMSelection) -> None:
        """Persist a global swap into the per-role map.

        Updates ``jarvis`` + ``subagent`` and keeps the other roles + stt +
        budget byte-for-byte. A missing file (never seeded — bare tests) fans
        the selection out to all four roles, like the boot seed.
        """

        current = self._selection_store.read()
        if current is None:
            next_map = RoleSelection(roles={role: selection for role in ROLES})
        else:
            next_map = current.with_role("jarvis", selection).with_role("subagent", selection)
        self._selection_store.write(next_map)

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
            current = self._read_flat()
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
            try:
                await asyncio.to_thread(
                    self._manager.load,
                    model_id,
                    effective_ctx,
                    reload=context_length is not None,
                )
            except LMStudioUnavailableError:
                # The lmstudio SDK management channel is unreachable. The SDK
                # pre-load is an OPTIMISATION — inference runs over the OpenAI
                # client, which JIT-loads the model on first use. So if the
                # OpenAI HTTP endpoint confirms the model is served (a remote /
                # OpenAI-only server that serves HTTP but not the SDK websocket),
                # proceed with the pin + client swap rather than hard-failing.
                # Otherwise re-raise — the server is genuinely unreachable.
                base_url = current.base_url if current is not None else None
                base_url = base_url or self._settings.LLM_BASE_URL
                if not (base_url and model_served_over_http(base_url, model_id)):
                    raise
                emit_debug(
                    category="llm",
                    severity="warn",
                    source="bob.llm_swap.swap_lm_model",
                    summary=(
                        f"SDK load unreachable for {model_id!r}; the OpenAI endpoint "
                        f"serves it, pinning for JIT inference (no pre-load)"
                    ),
                    payload={"model": model_id, "base_url": base_url},
                )

            # 2. Build the next selection (preserve provider + the ctx map, and
            #    pin an explicit ctx override for this model when supplied).
            new_selection = self._next_selection(current, model_id, context_length)

            # 3. Rebuild + swap both role clients from the NEW selection. Grab
            #    the superseded clients FIRST so the SDK transport's long-lived
            #    websockets (issue 0115) are torn down after the swap; closing
            #    happens AFTER both replacements are in place so no role is ever
            #    left clientless (Annexe G). No-op for OpenAI/Claude.
            old_jarvis = self._orchestrator.jarvis_client
            old_subagent = self._subagent_holder.client
            self._orchestrator.set_jarvis_client(build_jarvis_client(self._settings, new_selection))
            self._subagent_holder.set(build_subagent_client(self._settings, new_selection))

            # 4. Couple the bounded-context budget to the loaded ctx window.
            self._orchestrator.set_token_budget(token_budget_for_context_length(effective_ctx))

            # 5. Persist only after a fully successful swap.
            self._write_flat(new_selection)
            await self._aclose_superseded(old_jarvis, old_subagent)
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
            current = self._read_flat()

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
            #    Capture the superseded clients to close their SDK websockets
            #    (issue 0115) after the swap.
            old_jarvis = self._orchestrator.jarvis_client
            old_subagent = self._subagent_holder.client
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
            self._write_flat(new_selection)
            await self._aclose_superseded(old_jarvis, old_subagent)
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
            current = self._read_flat()
            new_host = host_from_base_url(url)

            self._manager.set_host(new_host)
            new_selection = self._next_base_url_selection(current, url)

            # A base_url change moves the SDK websocket host → the old clients'
            # long-lived connections (issue 0115) must be torn down after the swap.
            old_jarvis = self._orchestrator.jarvis_client
            old_subagent = self._subagent_holder.client
            self._orchestrator.set_jarvis_client(build_jarvis_client(self._settings, new_selection))
            self._subagent_holder.set(build_subagent_client(self._settings, new_selection))

            self._write_flat(new_selection)
            await self._aclose_superseded(old_jarvis, old_subagent)
            return SwapResult(selection=new_selection)

    @staticmethod
    async def _aclose_superseded(*clients: LLMClient | None) -> None:
        """Close every superseded client (no-op for non-SDK transports).

        Called AFTER both replacement clients are in place so neither role is
        ever momentarily without a client (Annexe G). Distinct objects only — if
        the same object backs both roles it is closed once.
        """

        seen: set[int] = set()
        for client in clients:
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            await aclose_client(client)

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

    def set(self, role: str, client: LLMClient) -> LLMClient | None:
        """Replace ``role``'s client, fire its sink, and return the OLD client.

        The superseded client is returned (``None`` if the role had none) so the
        async caller can :func:`aclose_client` it AFTER the replacement is in
        place — the SDK transport's long-lived websocket (issue 0115) must be
        torn down on supersede, while the role is never momentarily left without
        a usable client (Annexe G "jamais 0 modèle pour un rôle actif"). Closing
        is a no-op for the OpenAI / Claude clients (they hold no async resource).
        """

        if role not in ROLES:
            raise KeyError(f"Unknown LLM role: {role!r}")
        previous = self._clients.get(role)
        self._clients[role] = client
        sink = self._sinks.get(role)
        if sink is not None:
            sink(client)
        return previous


class RoleManagerRegistry:
    """Resolve the per-HOST :class:`LMStudioManager` for a role's ``base_url``.

    PRD 0016 / issue 0107, Annexe J step 6 (reassignment). The multi-load policy
    is **per host** (a manager owns one host's ref-count + budget), but a role
    pins its own ``base_url`` and may move between hosts on a swap. This registry
    maps a derived ``host:port`` to its manager so :meth:`RoleLLMSwitcher.swap_role`
    can ``assign_role`` / ``release_role`` on the RIGHT host without the switcher
    knowing how managers are built.

    A ``factory`` builds a manager for a host on first use; pre-seeded managers
    (the boot path) are passed in ``managers``. The registry is OPTIONAL on the
    switcher — when absent the per-role swap keeps its pre-0107 behaviour (no
    load, no budget check), so existing wiring is unchanged.
    """

    def __init__(
        self,
        managers: dict[str, LMStudioManager] | None = None,
        *,
        factory: Callable[[str], LMStudioManager] | None = None,
    ) -> None:
        self._managers: dict[str, LMStudioManager] = dict(managers or {})
        self._factory = factory

    def for_base_url(self, base_url: str | None) -> LMStudioManager:
        """Return (building on first use) the manager for ``base_url``'s host."""

        host = host_from_base_url(base_url)
        manager = self._managers.get(host)
        if manager is None:
            manager = self._factory(host) if self._factory is not None else LMStudioManager(host)
            self._managers[host] = manager
        return manager


class RoleLLMSwitcher:
    """Serialised coordinator for per-role selection swaps (rebuild ONE role).

    Owns the :class:`asyncio.Lock` serialising concurrent per-role swaps, the
    :class:`RoleSelectionStore`, and the :class:`RoleClientRegistry`. A
    :meth:`swap_role` call mutates exactly one role of the persisted map,
    rebuilds ONLY that role's client, pushes it through the registry (firing the
    role's sink), and persists the new map. The other three roles' selections
    and client objects round-trip untouched.

    With an OPTIONAL :class:`RoleManagerRegistry` (PRD 0016 / issue 0107) the
    swap also drives the per-host multi-load policy (Annexe J step 6): an
    ``lm_studio`` role with a pinned model is ``assign_role``-d on its host's
    manager (budget-checked, ref-counted) BEFORE the client rebuild, and its
    OLD model is released (evicted iff unreferenced). A budget refusal
    (:class:`bob.lm_studio_manager.ModelBudgetExceededError`) or a real OOM
    (:class:`bob.lm_studio_manager.LMStudioLoadError`) propagates BEFORE any
    rebuild / write, so the previous state stands (Annexe G "jamais 0 modèle pour
    un rôle actif"). Without the registry the swap keeps its pre-0107 behaviour.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        selection_store: RoleSelectionStore,
        registry: RoleClientRegistry,
        manager_registry: RoleManagerRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._selection_store = selection_store
        self._registry = registry
        self._manager_registry = manager_registry
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
        previous state. When a :class:`RoleManagerRegistry` is wired, also
        applies the per-host multi-load policy (load / budget-check / release)
        BEFORE the rebuild; a budget refusal or OOM there propagates and the
        previous state is kept.
        """

        if role not in ROLES:
            raise UnknownProviderError(f"Unknown LLM role: {role!r}")
        if selection.provider not in _VALID_PROVIDERS:
            raise UnknownProviderError(f"Unknown LLM provider: {selection.provider!r}")

        async with self._lock:
            current = self._selection_store.read()
            if current is None:
                current = _seed_role_selection_for_swap(self._settings)

            # Per-host multi-load policy FIRST (issue 0107). A budget refusal /
            # OOM raises here, BEFORE any client rebuild or persist, so the
            # previous selection + clients stand (Annexe G). Blocking SDK work
            # runs in a worker thread so the event loop is not parked.
            if self._manager_registry is not None:
                await asyncio.to_thread(
                    self._apply_load_policy, role, current.role(role), selection
                )

            next_map = current.with_role(role, selection)

            # Rebuild ONLY the changed role's client; the others are untouched.
            # ``set`` returns the superseded client; close it AFTER the
            # replacement is registered so the role is never momentarily without
            # a client (Annexe G), and the SDK transport's long-lived websocket
            # (issue 0115) is torn down instead of leaked. No-op for OpenAI/Claude.
            rebuilt = build_role_client(next_map, role, self._settings)
            previous = self._registry.set(role, rebuilt)

            self._selection_store.write(next_map)
            await aclose_client(previous)
            return next_map

    async def set_reasoning(self, role: str, reasoning: str | None) -> RoleSelection:
        """Update ONLY a role's reasoning level — no model load, no budget check.

        LM Studio's ``reasoning`` is a per-REQUEST chat param (sent in the chat
        body), NOT a load-time setting, so changing it must NOT reload the model
        or run the per-host budget policy — unlike :meth:`swap_role`. Lock-guarded:
        reads the map, replaces the role's selection with a copy carrying the new
        level (provider / model / base_url / context_length untouched), rebuilds
        ONLY that role's client so the live consumer picks up the new level via
        its sink, persists, and returns the full map. The rebuild reconstructs
        the cheap openai client object — it does NOT touch the LM Studio manager,
        so no model load / eviction / budget check happens.

        Raises :class:`UnknownProviderError` for an unknown role or an invalid
        reasoning level, BEFORE any write, so a bad request keeps the previous
        state.
        """

        if role not in ROLES:
            raise UnknownProviderError(f"Unknown LLM role: {role!r}")
        if reasoning is not None and reasoning not in REASONING_LEVELS:
            raise UnknownProviderError(f"Unknown reasoning level: {reasoning!r}")

        async with self._lock:
            current = self._selection_store.read()
            if current is None:
                current = _seed_role_selection_for_swap(self._settings)

            updated = replace(current.role(role), reasoning=reasoning)
            next_map = current.with_role(role, updated)

            # Rebuild ONLY this role's client so the new level rides on the next
            # request. No manager / load policy — reasoning is request-scoped.
            # Close the superseded client AFTER the rebuild is registered (the
            # SDK transport bakes ``reasoning`` into the client, so a level change
            # rebuilds + supersedes the long-lived websocket client; issue 0115).
            rebuilt = build_role_client(next_map, role, self._settings)
            previous = self._registry.set(role, rebuilt)

            self._selection_store.write(next_map)
            await aclose_client(previous)

            # Instrumentation (Debug View): make it explicit that a reasoning
            # change persists + refreshes the client only — it issues NO LM
            # Studio load. If a reload is observed, it is LM Studio applying the
            # new level on the NEXT inference call, not this swap.
            emit_debug(
                category="llm",
                severity="info",
                source="bob.llm_swap.set_reasoning",
                summary=(
                    f"reasoning {role}={reasoning!r} — persist + client refresh only, no model load"
                ),
                payload={
                    "role": role,
                    "reasoning": reasoning,
                    "lm_model": updated.lm_model,
                    "base_url": updated.base_url,
                    "model_loaded": False,
                },
            )
            return next_map

    def _apply_load_policy(self, role: str, previous: LLMSelection, target: LLMSelection) -> None:
        """Drive the per-host multi-load policy for a role reassignment.

        - target is ``lm_studio`` with a model → ``assign_role`` on the target
          host's manager (budget-checked, ref-counted). If the role previously
          used a DIFFERENT host, release it there too (the new host's manager
          owns the new reference; the old host evicts iff unreferenced). On the
          SAME host ``assign_role`` already releases the old model first.
        - target is ``claude_cli`` (or unpinned) → release the role from its
          previous lm_studio host (frees the old model iff unreferenced); no load.

        Raises :class:`ModelBudgetExceededError` / :class:`LMStudioLoadError` /
        :class:`LMStudioUnavailableError` unchanged for the route to map.
        """

        assert self._manager_registry is not None  # guarded by the caller
        prev_host_changed = (
            previous.provider == "lm_studio"
            and previous.lm_model
            and host_from_base_url(previous.base_url) != host_from_base_url(target.base_url)
        )
        if target.provider == "lm_studio" and target.lm_model:
            if prev_host_changed:
                self._manager_registry.for_base_url(previous.base_url).release_role(role)
            manager = self._manager_registry.for_base_url(target.base_url)
            ctx = target.context_length.get(target.lm_model)
            manager.assign_role(role, target.lm_model, ctx)
        elif previous.provider == "lm_studio" and previous.lm_model:
            # Role left LM Studio — release its old model on the old host.
            self._manager_registry.for_base_url(previous.base_url).release_role(role)


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
