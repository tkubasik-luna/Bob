"""Deep module owning LM Studio *model management* (PRD 0012 / issue 0079; v2 0107).

This module is the single boundary onto the official ``lmstudio`` Python SDK.
It is used ONLY for management concerns — listing the locally downloaded
models, their metadata, and (v2) loading / evicting role models with a
ref-counted, budget-aware policy. Inference still runs through
:mod:`bob.llm_client` on the ``openai`` client; the two never share a code path.

Public surface:

- :class:`LMStudioModel` — a value object describing one chat-capable model:
  ``id``, ``quantisation``, ``architecture``, ``max_context_length``,
  ``loaded``.
- :class:`LMStudioManager` — per-host management view: ``list_models`` /
  ``loaded_model_ids`` / ``probe`` (reads) and the v2 **multi-load** policy
  (``assign_role`` / ``release_role`` / ``reconcile`` / ``load``).
- :class:`LMStudioUnavailableError` / :class:`LMStudioModelNotFoundError` /
  :class:`LMStudioLoadError` / :class:`ModelBudgetExceededError` — DISTINCT,
  catchable errors so the REST layer maps each cleanly (Annexe G).

LMStudioManager v2 — multi-load vs the old offload-first (issue 0107)
--------------------------------------------------------------------

The pre-0107 ``load()`` was **offload-first**: it evicted EVERY resident model
before loading the target, so only one model was ever resident (anti-OOM, the
2026-06-05 robustness call). The real-time agent (PRD 0016) needs DISTINCT
models resident per role (jarvis / thinker / draft) at once, so v2 replaces
offload-first with **multi-load + ref-counted selective offload**:

- Each role references at most one model on this host. The manager keeps a
  ``model_id -> {roles}`` ref-count map.
- Loading a model for a role keeps every OTHER role's model resident. A model
  is evicted ONLY when the LAST role referencing it drops it (ref-count → 0).
- Re-selecting an already-resident model for another role just bumps the
  ref-count — it never evicts the others.
- Before any NEW load the manager sums resident footprints (:mod:`bob.model_budget`)
  and **refuses + raises** :class:`ModelBudgetExceededError` if the host
  ceiling would be exceeded (Annexe G "Budget dépassé"). On a real OOM despite
  a passing budget check the previous state is kept and the role's swap is
  refused (Annexe G "OOM au load").

The reversion from offload-first is made SAFE by the budget guard-rail: the
manager never loads past the ceiling, so two roles can be resident together
without the OOM flakiness offload-first was protecting against.

The SDK is faked at this boundary in tests (see
``tests/test_lm_studio_manager.py``), so the test suite is fully offline and
deterministic — no running LM Studio server is required.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx
import lmstudio

if TYPE_CHECKING:
    from bob.model_budget import HostBudget

#: Default LM Studio API host. The SDK falls back to its own discovery when
#: ``None`` is passed; we keep an explicit default so a misconfigured host is
#: visible at the call site.
DEFAULT_LM_STUDIO_HOST = "localhost:1234"

#: Coarse fallback footprint (GiB) booked for a model whose on-disk size could
#: not be probed (unknown path / remote catalogue). Conservative so an
#: un-sized model still consumes a realistic chunk of the budget rather than
#: appearing free. Only used by the budget check when no hint / probe value is
#: available.
DEFAULT_MODEL_FOOTPRINT_GIB = 5.0


#: Loopback host spellings that all denote "this machine". Collapsed to a single
#: canonical host so two URL spellings of the same local server (the UI's
#: ``localhost`` default vs a ``127.0.0.1`` / ``::1`` `.env`) don't spawn two
#: managers — each with its own empty ref-count — and reload the same model.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_CANONICAL_LOOPBACK = "localhost"


def host_from_base_url(base_url: str | None) -> str:
    """Derive the ``lmstudio`` SDK ``host:port`` from the inference ``LLM_BASE_URL``.

    Inference runs on the ``openai`` client against an OpenAI-compatible URL like
    ``http://192.168.86.21:1234/v1``; the management SDK wants the bare
    ``host:port`` (no scheme, no ``/v1`` path). Both must point at the same server,
    so the manager host is derived from the same setting rather than configured
    twice. Falls back to :data:`DEFAULT_LM_STUDIO_HOST` when the URL is absent or
    has no network location.

    Canonicalises the result so equivalent spellings map to one key: the host is
    lower-cased and every loopback alias (``localhost`` / ``127.0.0.1`` / ``::1``
    / ``0.0.0.0``, and any ``127.0.0.0/8`` address) collapses to ``localhost``.
    Without this the same local server reached as ``localhost:1234`` and
    ``127.0.0.1:1234`` got two managers and the model loaded twice (bug:
    duplicate model loads). Remote hosts are preserved verbatim (lower-cased) —
    we never assume two distinct IPs are the same machine.
    """

    if not base_url:
        return DEFAULT_LM_STUDIO_HOST
    parsed = urlsplit(base_url if "//" in base_url else f"//{base_url}")
    netloc = parsed.netloc
    if not netloc:
        return DEFAULT_LM_STUDIO_HOST
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        host = _CANONICAL_LOOPBACK
    if not host:
        return netloc.lower()
    port = parsed.port
    return f"{host}:{port}" if port is not None else host


# --- OpenAI-compatible HTTP probe (the channel inference actually uses) ------
#
# The lmstudio SDK talks to a SEPARATE management surface (a websocket API) from
# the OpenAI-compatible REST endpoint Bob runs inference against. A remote LM
# Studio (or any OpenAI-compatible server) can serve the HTTP API over the LAN
# while the SDK websocket is down / version-incompatible — so an SDK-only
# reachability probe falsely reports such a server "injoignable" even though
# inference would work. These helpers probe / list over the SAME HTTP endpoint
# the ``openai`` client uses, so the picker's "online" verdict matches reality.

_OPENAI_PROBE_TIMEOUT_S = 3.0
_OPENAI_LIST_TIMEOUT_S = 6.0


def _openai_models_url(base_url: str) -> str:
    """``{base_url}/models`` — the OpenAI list-models endpoint (cheap GET)."""

    return f"{base_url.rstrip('/')}/models"


def openai_endpoint_reachable(base_url: str, *, timeout: float = _OPENAI_PROBE_TIMEOUT_S) -> bool:
    """True when the OpenAI-compatible server answers ``GET {base_url}/models``.

    The authoritative "can Bob talk to this URL" check — it probes the exact
    channel inference uses, independent of the lmstudio SDK websocket. Never
    raises (a transport error / non-2xx collapses to ``False``)."""

    try:
        response = httpx.get(_openai_models_url(base_url), timeout=timeout)
    except httpx.HTTPError:
        return False
    return response.is_success


def list_models_via_openai(
    base_url: str, *, timeout: float = _OPENAI_LIST_TIMEOUT_S
) -> list[LMStudioModel]:
    """List models from the OpenAI ``GET {base_url}/models`` endpoint.

    FALLBACK for when the lmstudio SDK management channel is unreachable: the
    OpenAI shape carries only model ids (no quant / arch / ctx / loaded — that
    metadata lives on the SDK), so those fields come back ``None`` / ``False``,
    but the picker can still list + select a model for an OpenAI-only / remote
    server. Raises on a transport / non-2xx error so the caller surfaces
    "unavailable" rather than an empty list."""

    response = httpx.get(_openai_models_url(base_url), timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", []) if isinstance(payload, dict) else []
    models: list[LMStudioModel] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if isinstance(model_id, str) and model_id:
            models.append(
                LMStudioModel(
                    id=model_id,
                    quantisation=None,
                    architecture=None,
                    max_context_length=None,
                    loaded=False,
                )
            )
    return models


def model_served_over_http(
    base_url: str, model_id: str, *, timeout: float = _OPENAI_PROBE_TIMEOUT_S
) -> bool:
    """True when ``{base_url}/models`` lists ``model_id`` (best-effort, never raises).

    Used to decide whether a model is usable for inference over the OpenAI HTTP
    channel even when the lmstudio SDK management load failed (remote / OpenAI-
    only server). A transport / parse error collapses to ``False``."""

    try:
        return any(m.id == model_id for m in list_models_via_openai(base_url, timeout=timeout))
    except (httpx.HTTPError, ValueError):
        return False


#: ``LlmInfo.type`` values we treat as chat-capable. ``vlm`` (vision LLM) is a
#: chat model with image input; ``embedding`` models are excluded.
_CHAT_MODEL_TYPES = frozenset({"llm", "vlm"})

#: Hosts treated as LOCAL (RAM is probeable). Anything else is remote (ceiling
#: from an override or skipped). ``host:port`` is matched on the bare host.
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_local_host(host: str) -> bool:
    """Whether ``host`` (``host:port``) is the local machine (RAM probeable)."""

    bare = host.rsplit(":", 1)[0] if ":" in host else host
    return bare in _LOCAL_HOSTNAMES


@dataclass(frozen=True)
class LMStudioModel:
    """One chat-capable LM Studio model and its management metadata.

    - ``id`` — the model key LM Studio identifies it by (e.g.
      ``qwen2.5-7b-instruct``).
    - ``quantisation`` — the GGUF quant format (e.g. ``Q4_K_M``), surfaced from
      the SDK ``format`` field; ``None`` when the SDK omits it.
    - ``architecture`` — the model family (e.g. ``qwen2``); ``None`` when absent.
    - ``max_context_length`` — the trained context window in tokens.
    - ``loaded`` — whether the model is currently resident in LM Studio.
    """

    id: str
    quantisation: str | None
    architecture: str | None
    max_context_length: int | None
    loaded: bool

    def as_dict(self) -> dict[str, object]:
        """Serialise to the REST JSON shape."""

        return {
            "id": self.id,
            "quantisation": self.quantisation,
            "architecture": self.architecture,
            "max_context_length": self.max_context_length,
            "loaded": self.loaded,
        }


class LMStudioUnavailableError(RuntimeError):
    """The LM Studio server could not be reached.

    Raised by :meth:`LMStudioManager.list_models` when the underlying SDK
    raises a connection-level error. Distinct from a programming bug so the
    REST layer can map it to an explicit HTTP error response rather than a
    500 traceback.
    """


class LMStudioModelNotFoundError(RuntimeError):
    """The requested model id is not a downloaded LM Studio model.

    Raised by :meth:`LMStudioManager.load` when the SDK reports the target
    ``model_id`` is unknown (no such download). Distinct from a load failure so
    the REST layer maps it to a 404-flavoured error and the swap path keeps the
    previous selection.
    """


class LMStudioLoadError(RuntimeError):
    """Loading the target model failed (e.g. out of memory).

    Raised by :meth:`LMStudioManager.load` when the SDK accepts the model id
    but the load itself fails — most commonly VRAM/RAM exhaustion (OOM). The
    swap path catches this, keeps the previous selection and does NOT persist
    the JSON.
    """


class ModelBudgetExceededError(RuntimeError):
    """Loading the target would exceed the host's resident-memory ceiling.

    Raised by :meth:`LMStudioManager.assign_role` / :meth:`LMStudioManager.load`
    BEFORE any SDK load when the projected sum of resident footprints exceeds
    the host ceiling (:mod:`bob.model_budget`). Distinct from
    :class:`LMStudioLoadError` (a *real* OOM at the SDK): this is the proactive
    budget refusal (Annexe G "Budget dépassé (check)"). The REST layer maps it
    to a 4xx with the "dépasse le plafond, libère un rôle" message; nothing is
    loaded or evicted, so the previous state stands.
    """


class _SDKModelInfo(Protocol):
    """Structural view of the SDK ``info`` object we read from.

    The real object is a ``msgspec`` struct (``LlmInfo``); the test fake is a
    plain object exposing the same attributes. We only read; never mutate.
    """

    type: str
    model_key: str
    format: str | None
    architecture: str | None
    max_context_length: int | None
    vision: bool


class _SDKDownloadedModel(Protocol):
    """Structural view of one entry returned by ``list_downloaded_models``."""

    info: _SDKModelInfo


class _SDKLoadedModel(Protocol):
    """Structural view of one entry returned by ``list_loaded_models``."""

    identifier: str


class _SDKLlmNamespace(Protocol):
    """The subset of the SDK ``client.llm`` session surface we depend on.

    ``load_new_instance`` loads a downloaded model into memory; ``unload``
    evicts a currently-loaded instance by its identifier. Both are management
    calls — inference still runs through :mod:`bob.llm_client` on ``openai``.
    """

    def load_new_instance(
        self,
        model_key: str,
        *,
        config: dict[str, object] | None = ...,
    ) -> object: ...

    def unload(self, model_identifier: str) -> None: ...


class _SDKClient(Protocol):
    """The subset of the ``lmstudio`` client surface we depend on."""

    @property
    def llm(self) -> _SDKLlmNamespace: ...

    def list_downloaded_models(self) -> Sequence[_SDKDownloadedModel]: ...

    def list_loaded_models(self) -> Sequence[_SDKLoadedModel]: ...

    def close(self) -> None: ...


#: Factory type for the SDK client. The default builds a real
#: ``lmstudio.Client``; tests inject a fake to stay offline.
ClientFactory = Callable[[str], "_SDKClient"]


def _default_client_factory(host: str) -> _SDKClient:
    """Build a real ``lmstudio.Client`` for ``host``."""

    return lmstudio.Client(host)  # type: ignore[return-value]


@dataclass(frozen=True)
class RoleLoadResult:
    """Per-role outcome of a boot / reconcile pass (Annexe J step 5).

    ``role`` is ready when its model is resident (or it is a ``claude_cli`` role
    needing no load); offline when the load was refused (budget), failed (OOM)
    or the host was unreachable. ``detail`` carries a short reason for the
    picker / logs (never raised — :meth:`LMStudioManager.reconcile` collects
    these so one bad role never aborts the others).
    """

    role: str
    model_id: str | None
    ready: bool
    detail: str = ""


class LMStudioManager:
    """Management-only view onto ONE LM Studio host (v2 multi-load).

    The constructor takes the API host, an optional client factory (the DI seam
    tests use to inject a fake SDK client) and an optional resident-memory
    budget (:class:`bob.model_budget.HostBudget`). Inference is out of scope.

    v2 state (issue 0107):

    - ``_refs`` — ``model_id -> {roles referencing it}``. The ref-count that
      decides when a model may be evicted (only when the set empties).
    - ``_role_model`` — ``role -> model_id`` currently assigned on this host.
    - ``_budget`` — the per-host ceiling tracker; ``None`` disables the check
      (legacy / unbudgeted manager → behaves like "no ceiling").

    Concurrency note: the SDK calls are blocking and the v2 mutators are meant
    to be driven from the swap coordinator's ``asyncio.Lock`` (one host mutation
    at a time), so the manager itself keeps no internal lock — its in-memory
    maps are only touched from that serialised path.
    """

    def __init__(
        self,
        host: str = DEFAULT_LM_STUDIO_HOST,
        client_factory: ClientFactory | None = None,
        *,
        budget: HostBudget | None = None,
        model_footprint: Callable[[str, int | None], float] | None = None,
    ) -> None:
        self._host = host
        self._client_factory = client_factory or _default_client_factory
        # ``HostBudget`` is imported under TYPE_CHECKING only — model_budget
        # imports host_from_base_url from here, so keep the runtime edge
        # one-way. ``None`` disables the budget check (legacy / unbudgeted).
        self._budget = budget
        self._model_footprint = model_footprint
        self._refs: dict[str, set[str]] = {}
        self._role_model: dict[str, str] = {}

    @property
    def host(self) -> str:
        """The configured LM Studio API host."""

        return self._host

    def set_host(self, host: str) -> None:
        """Repoint the manager at a new ``host:port`` (runtime URL swap).

        Each management call opens a fresh client at ``self._host``, so a later
        call simply targets the new host — no persistent connection to rebuild.
        Used by :meth:`bob.llm_swap.LLMSwitcher.swap_base_url`.
        """

        self._host = host

    # --- read surface (unchanged from v1) -----------------------------------

    def probe(self) -> bool:
        """Return whether the LM Studio server is reachable (a real ping).

        A lightweight management round-trip (``list_loaded_models``) that never
        raises: an unreachable server collapses to ``False``. Backs the
        ``GET /api/llm/ping`` health check the picker uses to confirm "online".
        """

        try:
            self.loaded_model_ids()
        except LMStudioUnavailableError:
            return False
        return True

    def list_models(self) -> list[LMStudioModel]:
        """Return the live list of chat-capable downloaded models.

        Filters to ``type`` in ``{"llm", "vlm"}`` (embeddings excluded), and
        marks each model ``loaded`` when its key matches a currently loaded
        model identifier.

        Raises :class:`LMStudioUnavailableError` when the server is unreachable
        — the caller never sees a raw SDK error.
        """

        client = self._open_client()
        try:
            downloaded = client.list_downloaded_models()
            loaded_ids = self._loaded_ids(client)
        except lmstudio.LMStudioError as exc:
            raise LMStudioUnavailableError(
                f"LM Studio server unreachable at {self._host!r}: {exc}"
            ) from exc
        finally:
            self._safe_close(client)

        models: list[LMStudioModel] = []
        for entry in downloaded:
            info = getattr(entry, "info", None)
            if info is None:
                continue
            if getattr(info, "type", None) not in _CHAT_MODEL_TYPES:
                continue
            model_id = getattr(info, "model_key", None)
            if not isinstance(model_id, str) or not model_id:
                continue
            models.append(
                LMStudioModel(
                    id=model_id,
                    quantisation=_opt_str(getattr(info, "format", None)),
                    architecture=_opt_str(getattr(info, "architecture", None)),
                    max_context_length=_opt_int(getattr(info, "max_context_length", None)),
                    loaded=model_id in loaded_ids,
                )
            )
        return models

    def loaded_model_ids(self) -> list[str]:
        """Return the ids of models currently loaded in LM Studio.

        Used for cold-start resolution (issue 0080): when the selection pins no
        model, the boot path prefers an already-loaded model over loading a new
        one. Order follows the SDK's ``list_loaded_models`` order.

        Raises :class:`LMStudioUnavailableError` when the server is unreachable.
        """

        client = self._open_client()
        try:
            loaded = client.list_loaded_models()
        except lmstudio.LMStudioError as exc:
            raise LMStudioUnavailableError(
                f"LM Studio server unreachable at {self._host!r}: {exc}"
            ) from exc
        finally:
            self._safe_close(client)

        ids: list[str] = []
        for handle in loaded:
            identifier = getattr(handle, "identifier", None)
            if isinstance(identifier, str) and identifier:
                ids.append(identifier)
        return ids

    # --- v2 ref-count + budget surface (issue 0107) -------------------------

    def ref_count(self, model_id: str) -> int:
        """How many roles currently reference ``model_id`` on this host."""

        return len(self._refs.get(model_id, ()))

    def roles_for(self, model_id: str) -> frozenset[str]:
        """The set of roles currently referencing ``model_id``."""

        return frozenset(self._refs.get(model_id, frozenset()))

    def model_for_role(self, role: str) -> str | None:
        """The model id currently assigned to ``role`` on this host (or ``None``)."""

        return self._role_model.get(role)

    def resident_model_ids(self) -> frozenset[str]:
        """The set of model ids the manager tracks as resident (ref-count ≥ 1)."""

        return frozenset(self._refs)

    def assign_role(
        self,
        role: str,
        model_id: str,
        context_length: int | None = None,
        *,
        reload: bool = False,
    ) -> None:
        """Assign ``model_id`` to ``role`` — multi-load, ref-counted, budget-aware.

        The v2 replacement for the old offload-first ``load``:

        1. If ``role`` already points at a DIFFERENT model, release that old
           reference first (which may evict the old model iff no other role
           still holds it — selective, ref-counted offload).
        2. Reconcile against the server's LIVE loaded set: a model resident on
           the server is adopted / ref-bumped (NO eviction of peers, NO reload)
           — the "re-selecting a loaded model doesn't offload the others"
           invariant — unless ``reload`` forces a fresh load at a new context
           window. The in-process ref map alone is never trusted for this: it
           can claim residency for a model that was since ejected from the LM
           Studio UI (or lost to a server restart), and skipping the load on
           that stale claim left the role pointing at an unloaded model.
        3. Otherwise budget-check the NEW resident set (resident + candidate ≤
           ceiling). Over budget → raise :class:`ModelBudgetExceededError`
           BEFORE touching the SDK (nothing loaded / evicted).
        4. Load the target via the SDK, KEEPING every other resident model. A
           real OOM raises :class:`LMStudioLoadError` and the ref-count / budget
           are left exactly as before the attempt (Annexe G "OOM au load").

        Blocking (SDK call). Drive from the swap coordinator's lock.
        """

        current = self._role_model.get(role)
        if current is not None and (current != model_id or reload):
            # Re-pointing the role: drop the old ref (may evict the old model).
            self.release_role(role)

        if not reload:
            # Reconcile against the server's live loaded set — in BOTH
            # directions. The ref map starts EMPTY at boot while LM Studio may
            # have already JIT-loaded the model (reloading it would be the
            # "model loaded twice" bug), and it can be STALE the other way: the
            # refs book a model the server no longer holds (ejected from the LM
            # Studio UI / server restart), and adopting that claim without a
            # load left the role pointing at an unloaded model. Best-effort: if
            # the server can't be listed, fall through to the normal load path
            # (which surfaces its own error).
            try:
                server_loaded = model_id in self.loaded_model_ids()
            except LMStudioUnavailableError:
                server_loaded = False
            if server_loaded:
                already_booked = model_id in self._refs
                self._refs.setdefault(model_id, set()).add(role)
                self._role_model[role] = model_id
                if already_booked:
                    # Pure ref-count bump — footprint already booked.
                    self._budget_add(model_id, context_length)
                else:
                    # The model physically fits (it is already loaded) — book
                    # its footprint to reflect reality without a redundant
                    # budget gate.
                    self._budget_set(model_id, self._footprint_for(model_id, context_length))
                return
            if model_id in self._refs:
                # Stale residency claim — drop the phantom footprint so the
                # budget check below gates against reality, then load for real.
                self._budget_remove(model_id)

        # A NEW load (or a forced reload): budget-check FIRST. On a forced
        # reload of an already-resident model its own footprint is already
        # counted, so check_add reports it as fitting (candidate 0) — the reload
        # does not grow the resident set.
        candidate_gib = self._footprint_for(model_id, context_length)
        self._check_budget(model_id, candidate_gib)

        self._sdk_load(model_id, context_length, reload=reload)

        # Load succeeded — record the reference + the resident footprint.
        self._refs.setdefault(model_id, set()).add(role)
        self._role_model[role] = model_id
        self._budget_set(model_id, candidate_gib)

    def release_role(self, role: str) -> None:
        """Drop ``role``'s reference; evict its model iff no role still holds it.

        Selective, ref-counted offload: the model is unloaded from the SDK ONLY
        when this was the last role referencing it. A model still referenced by
        another role stays resident. A role with no current assignment is a
        no-op. Best-effort unload — an SDK unload failure is swallowed (the
        ref-count is the source of truth).
        """

        model_id = self._role_model.pop(role, None)
        if model_id is None:
            return
        refs = self._refs.get(model_id)
        if refs is None:
            return
        refs.discard(role)
        if refs:
            # Still referenced by another role — keep it resident.
            return
        # Last reference gone — evict it from the host.
        del self._refs[model_id]
        self._budget_remove(model_id)
        self._unload(model_id)

    def reconcile(
        self, role_models: Mapping[str, tuple[str | None, int | None]]
    ) -> list[RoleLoadResult]:
        """Boot / re-load sequence for THIS host's roles (Annexe J steps 3-5).

        ``role_models`` maps each role assigned to this host to its
        ``(model_id, context_length)`` (a ``None`` model id = a role with no LM
        Studio model on this host, e.g. ``claude_cli`` — marked ready, no load).

        Per role, in iteration order: assign the model (budget-checked,
        ref-counted multi-load) and mark ``ready``; a budget refusal / real OOM
        / unreachable host marks THAT role ``offline`` with a reason and moves
        on — one bad role never aborts its peers, and a role that was already
        ready is never torn down by a later failure (Annexe G "jamais 0 modèle
        pour un rôle actif"). Returns one :class:`RoleLoadResult` per role.
        """

        results: list[RoleLoadResult] = []
        for role, (model_id, context_length) in role_models.items():
            if model_id is None:
                results.append(RoleLoadResult(role=role, model_id=None, ready=True))
                continue
            try:
                self.assign_role(role, model_id, context_length)
            except ModelBudgetExceededError as exc:
                results.append(
                    RoleLoadResult(role=role, model_id=model_id, ready=False, detail=str(exc))
                )
            except LMStudioUnavailableError as exc:
                results.append(
                    RoleLoadResult(role=role, model_id=model_id, ready=False, detail=str(exc))
                )
            except (LMStudioLoadError, LMStudioModelNotFoundError) as exc:
                results.append(
                    RoleLoadResult(role=role, model_id=model_id, ready=False, detail=str(exc))
                )
            else:
                results.append(RoleLoadResult(role=role, model_id=model_id, ready=True))
        return results

    def load(
        self, model_id: str, context_length: int | None = None, *, reload: bool = False
    ) -> None:
        """Load ``model_id`` into LM Studio — v2 MULTI-LOAD (keeps peers resident).

        BEHAVIOUR CHANGE (issue 0107): the pre-0107 ``load`` was offload-first
        (it evicted EVERY resident model before loading the target). v2 no longer
        evicts peers — it budget-checks the target against the resident set, then
        loads it while leaving every other model loaded:

        - Already resident + plain select (no ``reload``) → no-op (kept resident,
          peers untouched), exactly as before.
        - Over budget → raise :class:`ModelBudgetExceededError` BEFORE the load
          (nothing loaded / evicted).
        - Otherwise load the target via the SDK, keeping the other residents.
        - ``reload`` forces a fresh load even when the target is already resident
          (the ctx-slider Apply path); peers are still kept.

        This is the legacy single-selection entry point still used by
        :class:`bob.llm_swap.LLMSwitcher`. It does NOT touch the role ref-count
        map (that is the per-role :meth:`assign_role` surface); it only honours
        the budget + multi-load load policy at the SDK boundary.

        Errors are surfaced as DISTINCT, catchable types (Annexe G):
        unreachable → :class:`LMStudioUnavailableError`; unknown id →
        :class:`LMStudioModelNotFoundError`; load failed (OOM) →
        :class:`LMStudioLoadError`; over budget → :class:`ModelBudgetExceededError`.
        """

        client = self._open_client()
        try:
            previous = self._loaded_ids(client)
            if model_id in previous and not reload:
                # Already resident, plain select → keep it (and every peer).
                return
            # Budget-check against the models the SDK reports resident. A forced
            # reload of an already-resident model counts it as fitting (its
            # footprint is already part of the resident total). We sum a coarse
            # footprint per resident id since per-model context is not known on
            # this legacy path.
            candidate_gib = self._footprint_for(model_id, context_length)
            self._check_budget_against(previous, model_id, candidate_gib)
            # Forced reload: cycle ONLY the target (free its instance so the new
            # ctx window takes); peers stay resident (multi-load — NOT
            # offload-first). Best-effort unload.
            if reload and model_id in previous:
                with contextlib.suppress(lmstudio.LMStudioError):
                    client.llm.unload(model_id)
            self._load_via_client(client, model_id, context_length)
        finally:
            self._safe_close(client)

    # --- internals -----------------------------------------------------------

    def _sdk_load(self, model_id: str, context_length: int | None, *, reload: bool) -> None:
        """Open a client, load the target (no offload), close. Raises typed errors."""

        client = self._open_client()
        try:
            if reload and model_id in self._loaded_ids(client):
                with contextlib.suppress(lmstudio.LMStudioError):
                    client.llm.unload(model_id)
            self._load_via_client(client, model_id, context_length)
        finally:
            self._safe_close(client)

    @staticmethod
    def _load_via_client(client: _SDKClient, model_id: str, context_length: int | None) -> None:
        """Issue the SDK ``load_new_instance`` and map failures to typed errors."""

        config: dict[str, object] | None = None
        if context_length is not None:
            config = {"contextLength": context_length}
        try:
            client.llm.load_new_instance(model_id, config=config)
        except lmstudio.LMStudioModelNotFoundError as exc:
            raise LMStudioModelNotFoundError(
                f"LM Studio model not found: {model_id!r}: {exc}"
            ) from exc
        except lmstudio.LMStudioError as exc:
            # Server-side load failure — most commonly OOM. Distinct from the
            # unreachable-server case (the connection itself succeeded).
            raise LMStudioLoadError(f"LM Studio failed to load {model_id!r}: {exc}") from exc

    def _unload(self, model_id: str) -> None:
        """Best-effort SDK unload of ``model_id`` (swallows unreachable / errors)."""

        try:
            client = self._open_client()
        except LMStudioUnavailableError:
            return
        try:
            with contextlib.suppress(lmstudio.LMStudioError):
                client.llm.unload(model_id)
        finally:
            self._safe_close(client)

    def _open_client(self) -> _SDKClient:
        try:
            return self._client_factory(self._host)
        except lmstudio.LMStudioError as exc:
            raise LMStudioUnavailableError(
                f"LM Studio server unreachable at {self._host!r}: {exc}"
            ) from exc

    @staticmethod
    def _loaded_ids(client: _SDKClient) -> set[str]:
        loaded: set[str] = set()
        for handle in client.list_loaded_models():
            identifier = getattr(handle, "identifier", None)
            if isinstance(identifier, str) and identifier:
                loaded.add(identifier)
        return loaded

    @staticmethod
    def _safe_close(client: _SDKClient) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):  # pragma: no cover - defensive close
                close()

    # --- budget helpers (delegate to the per-host HostBudget) ----------------

    def _footprint_for(self, model_id: str, context_length: int | None) -> float:
        """Estimate ``model_id``'s footprint (GiB) via the injected probe / default."""

        if self._model_footprint is not None:
            return self._model_footprint(model_id, context_length)
        return DEFAULT_MODEL_FOOTPRINT_GIB

    def _check_budget(self, model_id: str, candidate_gib: float) -> None:
        """Raise :class:`ModelBudgetExceededError` if adding ``model_id`` over budget.

        Uses the per-host :class:`bob.model_budget.HostBudget` ref-count view (so
        a re-selected resident model fits). No budget configured → no check.
        """

        if self._budget is None:
            return
        decision = self._budget.check_add(model_id, candidate_gib)
        if not decision.ok:
            raise ModelBudgetExceededError(decision.message())

    def _check_budget_against(
        self, resident_ids: set[str], model_id: str, candidate_gib: float
    ) -> None:
        """Budget-check the legacy ``load`` path against the SDK-reported residents.

        The legacy single-selection ``load`` does not own the ref-count map, so it
        budgets against the SDK's ``list_loaded_models`` snapshot: a coarse
        footprint per already-resident peer plus the candidate. Already-resident
        target / no-budget → fits.
        """

        if self._budget is None:
            return
        if model_id in resident_ids:
            return
        from bob.model_budget import fits

        resident_total = sum(self._footprint_for(mid, None) for mid in resident_ids)
        if not fits([resident_total, candidate_gib], self._budget.ceiling_gib):
            ceiling = self._budget.ceiling_gib
            ceiling_str = "∞" if ceiling is None else f"{ceiling:.1f}"
            raise ModelBudgetExceededError(
                f"chargement de {model_id!r} refusé : dépasse le plafond mémoire "
                f"({resident_total + candidate_gib:.1f} GiB requis > {ceiling_str} GiB) — "
                f"libère un rôle pour ce host."
            )

    def _budget_set(self, model_id: str, footprint_gib: float) -> None:
        if self._budget is not None:
            self._budget.add(model_id, footprint_gib)

    def _budget_add(self, model_id: str, context_length: int | None) -> None:
        # Idempotent record for a ref-count bump of an already-resident model
        # (its footprint is already booked; re-recording keeps the value fresh).
        if self._budget is not None and not self._budget.is_resident(model_id):
            self._budget.add(model_id, self._footprint_for(model_id, context_length))

    def _budget_remove(self, model_id: str) -> None:
        if self._budget is not None:
            self._budget.remove(model_id)


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return None
    return value if isinstance(value, int) else None
