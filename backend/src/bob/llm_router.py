"""Read-only REST endpoints for the LLM selection (PRD 0012).

Issue 0078 exposed ``GET /api/llm/selection`` returning the current selection
owned by :class:`bob.llm_selection_store.LLMSelectionStore`.

Issue 0079 adds ``GET /api/llm/models`` returning the live list of locally
downloaded, chat-capable LM Studio models (via
:class:`bob.lm_studio_manager.LMStudioManager`). Both endpoints are read-only —
no mutation, no model loading, no client rebuild.

Each external dependency is resolved through a DI seam (mirroring
:mod:`bob.debug_router`) so tests can prime their own store / fake SDK manager
without running the full app lifespan.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bob.config import Settings, get_settings
from bob.llm_selection_store import (
    REASONING_LEVELS,
    ROLES,
    LLMSelection,
    LLMSelectionStore,
    RoleSelectionStore,
    get_default_role_store,
    get_default_store,
)
from bob.llm_swap import (
    ClaudeCliUnavailableError,
    LLMSwitcher,
    RoleLLMSwitcher,
    UnknownProviderError,
)
from bob.lm_studio_manager import (
    LMStudioLoadError,
    LMStudioManager,
    LMStudioModelNotFoundError,
    LMStudioUnavailableError,
    ModelBudgetExceededError,
    host_from_base_url,
    list_models_via_openai,
    openai_endpoint_reachable,
)

router = APIRouter(prefix="/api/llm", tags=["llm"])

# DI seam for the live-swap coordinator. Primed by the app lifespan (it needs
# the orchestrator + sub-agent holder). ``None`` until then; the PUT route
# returns 503 if it is called before boot wiring (tests inject their own).
_switcher: LLMSwitcher | None = None


def set_switcher(switcher: LLMSwitcher | None) -> None:
    """Install (or clear) the live-swap coordinator used by ``PUT /selection``."""

    global _switcher
    _switcher = switcher


# DI seam so the route test can swap the store factory without booting the
# whole app. Defaults to the process-wide singleton primed by the lifespan.
_store_provider: Callable[[], LLMSelectionStore] = get_default_store

# DI seam for the LM Studio management client. Defaults to a fresh manager
# pointing at the local server; tests inject one wired to a fake SDK client.
_manager_provider: Callable[[], LMStudioManager] = LMStudioManager

# DI seam for settings. The Claude model label (``GET /selection``) is read
# from ``CLAUDE_CLI_MODEL``; tests override this without booting the app.
_settings_provider: Callable[[], Settings] = get_settings


def set_settings_provider(provider: Callable[[], Settings]) -> None:
    """Override the settings factory used to surface the Claude model label."""

    global _settings_provider
    _settings_provider = provider


def reset_settings_provider() -> None:
    """Restore the default settings factory."""

    global _settings_provider
    _settings_provider = get_settings


def set_store_provider(provider: Callable[[], LLMSelectionStore]) -> None:
    """Override the selection-store factory used by the endpoints."""

    global _store_provider
    _store_provider = provider


def reset_store_provider() -> None:
    """Restore the default selection-store factory (the singleton)."""

    global _store_provider
    _store_provider = get_default_store


def set_manager_provider(provider: Callable[[], LMStudioManager]) -> None:
    """Override the LM Studio manager factory used by ``GET /models``."""

    global _manager_provider
    _manager_provider = provider


def reset_manager_provider() -> None:
    """Restore the default LM Studio manager factory."""

    global _manager_provider
    _manager_provider = LMStudioManager


# --- Per-role DI seams (PRD 0016 / issue 0106) -------------------------------
#
# The per-role endpoints (``GET /api/llm/roles`` + ``PUT /api/llm/roles/{role}``)
# resolve the v2 :class:`RoleSelectionStore` and the per-role swap coordinator
# through their own seams, mirroring the global ones above so route tests can
# prime fakes without booting the app lifespan.

_role_store_provider: Callable[[], RoleSelectionStore] = get_default_role_store

#: Per-role swap coordinator. Primed by the app lifespan (needs the orchestrator
#: + holders). ``None`` until then; the PUT route returns 503 if called before
#: boot wiring (tests inject their own).
_role_switcher: RoleLLMSwitcher | None = None


def set_role_store_provider(provider: Callable[[], RoleSelectionStore]) -> None:
    """Override the per-role selection-store factory used by the endpoints."""

    global _role_store_provider
    _role_store_provider = provider


def reset_role_store_provider() -> None:
    """Restore the default per-role selection-store factory (the singleton)."""

    global _role_store_provider
    _role_store_provider = get_default_role_store


def set_role_switcher(switcher: RoleLLMSwitcher | None) -> None:
    """Install (or clear) the per-role swap coordinator used by ``PUT /roles/{role}``."""

    global _role_switcher
    _role_switcher = switcher


#: Fallback Claude model label when ``CLAUDE_CLI_MODEL`` is unset, so the
#: picker's Claude side always has a non-empty read-only label to render.
DEFAULT_CLAUDE_MODEL_LABEL = "claude-sonnet-4.5"


class LLMSelectionResponse(BaseModel):
    """Body for ``GET /api/llm/selection``.

    ``claude_model`` (issue 0081) is the read-only model label the picker shows
    on the Claude CLI side — the configured ``CLAUDE_CLI_MODEL`` or a default.
    It is informational only: there is no Claude model dropdown.
    """

    provider: str
    lm_model: str | None
    context_length: dict[str, int]
    claude_model: str
    base_url: str | None = None


@router.get("/selection", response_model=LLMSelectionResponse)
def get_llm_selection() -> LLMSelectionResponse:
    """Return the current LLM selection.

    Reads through the store. The store is guaranteed seeded by the boot path
    (:func:`LLMSelectionStore.seed_from_settings`), so ``read`` never returns
    ``None`` in the running app; the response falls back to the seeded values
    regardless.
    """

    store = _store_provider()
    selection = store.read()
    provider = selection.provider if selection is not None else "lm_studio"
    lm_model = selection.lm_model if selection is not None else None
    context_length = selection.context_length if selection is not None else {}
    settings = _settings_provider()
    # Report the EFFECTIVE base URL: the picker must show the server actually in
    # use. A selection with no pinned base_url (legacy JSON / .env-seeded) falls
    # back to the active LLM_BASE_URL rather than a UI placeholder.
    base_url = (selection.base_url if selection is not None else None) or (
        settings.LLM_BASE_URL or None
    )
    claude_model = settings.CLAUDE_CLI_MODEL or DEFAULT_CLAUDE_MODEL_LABEL
    return LLMSelectionResponse(
        provider=provider,
        lm_model=lm_model,
        context_length=context_length,
        claude_model=claude_model,
        base_url=base_url,
    )


class LLMModel(BaseModel):
    """One chat-capable LM Studio model in the ``GET /models`` list."""

    id: str
    quantisation: str | None
    architecture: str | None
    max_context_length: int | None
    loaded: bool


class LLMModelsResponse(BaseModel):
    """Body for ``GET /api/llm/models`` (success)."""

    models: list[LLMModel]


class LLMModelsErrorResponse(BaseModel):
    """Body for ``GET /api/llm/models`` when LM Studio is unreachable."""

    error: str
    detail: str


@router.get(
    "/models",
    response_model=LLMModelsResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": LLMModelsErrorResponse}},
)
def get_llm_models(base_url: str | None = None) -> JSONResponse:
    """Return the live list of chat-capable LM Studio models.

    With ``?base_url=`` set, lists the CANDIDATE server's models (used by the
    per-role picker so each role's model list follows its OWN typed/committed
    URL, not the globally-configured server). Without it, lists the
    currently-configured server's models.

    Embedding models are excluded by the manager. When the LM Studio server is
    unreachable, returns HTTP 503 with a DISTINCT, structured error body rather
    than letting the SDK error bubble into a 500 traceback — the frontend can
    show "serveur LM Studio injoignable" and the picker degrades gracefully.
    """

    if base_url:
        manager: LMStudioManager = LMStudioManager(host=host_from_base_url(base_url))
    else:
        manager = _manager_provider()
    try:
        models = manager.list_models()
    except LMStudioUnavailableError as exc:
        # The lmstudio SDK management channel is down. For a candidate URL (the
        # picker / setup screen) fall back to the OpenAI HTTP list so an
        # OpenAI-only / remote server still surfaces selectable models (ids only —
        # no SDK metadata). Only the SDK can list when no base_url is given.
        if base_url:
            try:
                models = list_models_via_openai(base_url)
            except httpx.HTTPError as http_exc:
                body = LLMModelsErrorResponse(
                    error="lm_studio_unavailable", detail=f"{exc}; HTTP fallback: {http_exc}"
                )
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content=body.model_dump(),
                )
        else:
            body = LLMModelsErrorResponse(error="lm_studio_unavailable", detail=str(exc))
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=body.model_dump(),
            )
    payload = LLMModelsResponse(
        models=[
            LLMModel(
                id=m.id,
                quantisation=m.quantisation,
                architecture=m.architecture,
                max_context_length=m.max_context_length,
                loaded=m.loaded,
            )
            for m in models
        ]
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload.model_dump())


class LLMPingResponse(BaseModel):
    """Body for ``GET /api/llm/ping`` — a real LM Studio reachability probe."""

    reachable: bool
    host: str


@router.get("/ping", response_model=LLMPingResponse)
def ping_llm(base_url: str | None = None) -> JSONResponse:
    """Probe whether an LM Studio server is reachable (a real online ping).

    With ``?base_url=`` set, probes that CANDIDATE server (used by the picker's
    URL field to confirm a typed/preset URL before committing it). Without it,
    probes the CURRENTLY-configured server. Always 200 with ``{reachable, host}``
    — never an error status — so the picker can render an online/offline chip
    without exception handling.
    """

    if base_url:
        host = host_from_base_url(base_url)
        # Probe the OpenAI HTTP endpoint FIRST — it's the channel inference uses,
        # and a remote server may serve it without the lmstudio SDK websocket the
        # SDK probe needs (which otherwise falsely reads "injoignable"). Fall back
        # to the SDK probe only when HTTP is unreachable, so a local LM Studio
        # with the SDK up but HTTP momentarily blocked still reads online.
        reachable = openai_endpoint_reachable(base_url) or LMStudioManager(host=host).probe()
    else:
        manager = _manager_provider()
        host = manager.host
        reachable = manager.probe()
    body = LLMPingResponse(reachable=reachable, host=host)
    return JSONResponse(status_code=status.HTTP_200_OK, content=body.model_dump())


class LLMSelectionUpdateRequest(BaseModel):
    """Body for ``PUT /api/llm/selection``.

    Two mutually-exclusive mutations are accepted:

    - ``lm_model`` — change the active LM Studio model (issue 0080).
    - ``provider`` — switch the active provider, Claude CLI ↔ LM Studio
      (issue 0081).

    Exactly one of ``lm_model`` / ``provider`` must be present; the route
    rejects a body with zero or both.

    ``context_length`` (issue 0082) is an OPTIONAL companion to ``lm_model``: it
    is the explicit ctx-slider Apply value, so the target model is loaded at that
    window and the budget couples to it. It is meaningless alongside
    ``provider`` (the route rejects that combination).
    """

    lm_model: str | None = None
    provider: str | None = None
    context_length: int | None = None
    base_url: str | None = None


class LLMSelectionUpdateErrorResponse(BaseModel):
    """Structured error body for a failed ``PUT /api/llm/selection``."""

    error: str
    detail: str


def _error_response(code: str, detail: str, http_status: int) -> JSONResponse:
    """Build a structured ``{error, detail}`` body at ``http_status``."""

    return JSONResponse(
        status_code=http_status,
        content=LLMSelectionUpdateErrorResponse(error=code, detail=detail).model_dump(),
    )


def _selection_payload(selection: object) -> JSONResponse:
    """Build the 200 body for a successful swap, including the Claude label."""

    # ``selection`` is an ``LLMSelection``; typed loosely to keep the import
    # surface here unchanged (the value object lives in the swap result).
    settings = _settings_provider()
    claude_model = settings.CLAUDE_CLI_MODEL or DEFAULT_CLAUDE_MODEL_LABEL
    base_url = selection.base_url or (settings.LLM_BASE_URL or None)  # type: ignore[attr-defined]
    payload = LLMSelectionResponse(
        provider=selection.provider,  # type: ignore[attr-defined]
        lm_model=selection.lm_model,  # type: ignore[attr-defined]
        context_length=selection.context_length,  # type: ignore[attr-defined]
        claude_model=claude_model,
        base_url=base_url,
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload.model_dump())


@router.put(
    "/selection",
    response_model=LLMSelectionResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": LLMSelectionUpdateErrorResponse},
        status.HTTP_409_CONFLICT: {"model": LLMSelectionUpdateErrorResponse},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": LLMSelectionUpdateErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": LLMSelectionUpdateErrorResponse},
    },
)
async def put_llm_selection(body: LLMSelectionUpdateRequest) -> JSONResponse:
    """Mutate the active LLM selection — synchronous, blocking, validate-then-swap.

    Dispatches on the body to exactly one mutation, each delegating to the
    :class:`bob.llm_swap.LLMSwitcher` (lock-serialised, validate-the-target-
    before-mutating, rebuild + swap BOTH role clients, then persist the JSON):

    - ``provider`` set (issue 0081) → switch Claude CLI ↔ LM Studio. Validates
      the target first (LM Studio reachable / ``claude`` on ``PATH``); on
      failure the previous provider is kept and nothing is written.
    - ``lm_model`` set (issue 0080) → change the active LM Studio model.

    Exactly one field must be present (zero or both → 422). Error → HTTP:

    - unknown model id → 404
    - load failed (OOM) → 409
    - unknown provider / invalid request → 422
    - LM Studio unreachable / ``claude`` missing / swap not wired → 503

    The generous load timeout lives in the SDK call; this route awaits it.
    """

    has_model = body.lm_model is not None
    has_provider = body.provider is not None
    has_base_url = body.base_url is not None
    if (has_model + has_provider + has_base_url) != 1:
        return _error_response(
            "invalid_request",
            "Exactly one of 'lm_model', 'provider' or 'base_url' must be provided",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if not has_model and body.context_length is not None:
        return _error_response(
            "invalid_request",
            "'context_length' is only valid alongside 'lm_model'",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if body.context_length is not None and body.context_length <= 0:
        return _error_response(
            "invalid_request",
            "'context_length' must be a positive integer",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if _switcher is None:
        return _error_response(
            "swap_unavailable",
            "LLM swap coordinator not initialised (app lifespan not running)",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if has_provider:
        return await _handle_provider_swap(body.provider or "")
    if has_base_url:
        return await _handle_base_url_swap(body.base_url or "")
    return await _handle_model_swap(body.lm_model or "", body.context_length)


async def _handle_model_swap(lm_model: str, context_length: int | None) -> JSONResponse:
    """Run the LM Studio model swap (issue 0080/0082) and map outcomes to HTTP.

    ``context_length`` (issue 0082) is the optional ctx-slider Apply value
    threaded into the load + per-model persistence + budget coupling.
    """

    assert _switcher is not None  # guarded by the caller
    model_id = lm_model.strip()
    if not model_id:
        return _error_response(
            "invalid_request",
            "lm_model must be a non-empty string",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        result = await _switcher.swap_lm_model(model_id, context_length)
    except LMStudioModelNotFoundError as exc:
        return _error_response("model_not_found", str(exc), status.HTTP_404_NOT_FOUND)
    except LMStudioLoadError as exc:
        return _error_response("load_failed", str(exc), status.HTTP_409_CONFLICT)
    except LMStudioUnavailableError as exc:
        return _error_response(
            "lm_studio_unavailable", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return _selection_payload(result.selection)


async def _handle_provider_swap(provider: str) -> JSONResponse:
    """Run the provider switch (issue 0081) and map outcomes to HTTP.

    The target is validated before any swap: LM Studio reachable for the
    ``lm_studio`` target, ``claude`` on ``PATH`` for ``claude_cli``. A
    validation failure keeps the previous provider and writes nothing.
    """

    assert _switcher is not None  # guarded by the caller
    provider_id = provider.strip()
    try:
        result = await _switcher.swap_provider(provider_id)
    except UnknownProviderError as exc:
        return _error_response("unknown_provider", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
    except ClaudeCliUnavailableError as exc:
        return _error_response(
            "claude_cli_unavailable", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE
        )
    except LMStudioUnavailableError as exc:
        return _error_response(
            "lm_studio_unavailable", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return _selection_payload(result.selection)


async def _handle_base_url_swap(base_url: str) -> JSONResponse:
    """Run the LM Studio base-URL swap and map outcomes to HTTP.

    The target is NOT probed: the user must be able to re-point the server
    even when the current one is dead. Reachability surfaces in the UI's ping
    chip post-swap.
    """

    assert _switcher is not None  # guarded by the caller
    url = base_url.strip()
    if not url:
        return _error_response(
            "invalid_request",
            "base_url must be a non-empty string",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    result = await _switcher.swap_base_url(url)
    return _selection_payload(result.selection)


# =============================================================================
# Per-role selection endpoints — PRD 0016 / issue 0106
# =============================================================================
#
# ``GET  /api/llm/roles``        → the full per-role map (+ stt + budget).
# ``PUT  /api/llm/roles/{role}`` → swap ONE role's selection; rebuilds ONLY that
#                                  role's client (the other three are untouched).
#
# These sit ALONGSIDE the global ``/selection`` endpoints (unchanged) so the
# pre-0016 picker keeps working while the per-role picker (frontend, later
# slice) targets the new surface.


class RoleSelectionBody(BaseModel):
    """One role's selection in the ``GET /roles`` map / the ``PUT`` body.

    Mirrors the flat :class:`bob.llm_selection_store.LLMSelection` on the wire:
    ``provider`` is ``lm_studio`` | ``claude_cli``; ``base_url`` / ``lm_model``
    are per-role (a role may pin its own server + model); ``context_length`` is
    the per-model ctx map round-tripped for budgeting.
    """

    provider: str
    base_url: str | None = None
    lm_model: str | None = None
    context_length: dict[str, int] = {}
    #: LM Studio reasoning level — ``"off"|"low"|"medium"|"high"|"on"`` or
    #: ``None`` (omit → the model's auto setting). Only meaningful for an
    #: ``lm_studio`` role; the store drops an out-of-range value to ``None``.
    reasoning: str | None = None


class SttSelectionBody(BaseModel):
    """The ``stt`` block in the ``GET /roles`` response."""

    engine: str
    model: str


class BudgetSelectionBody(BaseModel):
    """The ``budget`` block in the ``GET /roles`` response."""

    ceiling_gib: float | None = None
    reserve_gib: float
    per_host_override: dict[str, float] = {}


class RoleMapResponse(BaseModel):
    """Body for ``GET /api/llm/roles`` (and a successful ``PUT``).

    ``roles`` maps each role id to its :class:`RoleSelectionBody`; ``stt`` /
    ``budget`` carry the speech + model-budget blocks. ``claude_model`` is the
    read-only Claude label (mirrors ``GET /selection``) so a per-role Claude
    pick can render a model name without a separate fetch.
    """

    schema_version: int
    roles: dict[str, RoleSelectionBody]
    stt: SttSelectionBody
    budget: BudgetSelectionBody
    claude_model: str


def _role_map_payload(selection: object) -> RoleMapResponse:
    """Project a :class:`RoleSelection` onto the wire response."""

    # Typed loosely to keep the import surface small; ``selection`` is a
    # :class:`bob.llm_selection_store.RoleSelection`.
    settings = _settings_provider()
    claude_model = settings.CLAUDE_CLI_MODEL or DEFAULT_CLAUDE_MODEL_LABEL
    fallback_base_url = settings.LLM_BASE_URL or None
    roles: dict[str, RoleSelectionBody] = {}
    for role in ROLES:
        sel = selection.roles[role]  # type: ignore[attr-defined]
        # Report the EFFECTIVE base URL for LM Studio roles: the picker must show
        # the server actually in use. A role with no pinned base_url (default
        # seed / migrated flat file) falls back to the active LLM_BASE_URL rather
        # than letting the UI substitute a hardcoded localhost placeholder — that
        # mismatch was the "wrong URL shown" bug. Claude roles have no base_url,
        # so the fallback only applies to lm_studio (mirrors GET /selection).
        role_base_url = sel.base_url
        if sel.provider == "lm_studio" and not role_base_url:
            role_base_url = fallback_base_url
        roles[role] = RoleSelectionBody(
            provider=sel.provider,
            base_url=role_base_url,
            lm_model=sel.lm_model,
            context_length=sel.context_length,
            reasoning=sel.reasoning,
        )
    stt = selection.stt  # type: ignore[attr-defined]
    budget = selection.budget  # type: ignore[attr-defined]
    return RoleMapResponse(
        schema_version=selection.schema_version,  # type: ignore[attr-defined]
        roles=roles,
        stt=SttSelectionBody(engine=stt.engine, model=stt.model),
        budget=BudgetSelectionBody(
            ceiling_gib=budget.ceiling_gib,
            reserve_gib=budget.reserve_gib,
            per_host_override=budget.per_host_override,
        ),
        claude_model=claude_model,
    )


@router.get("/roles", response_model=RoleMapResponse)
def get_llm_roles() -> RoleMapResponse:
    """Return the full per-role LLM selection map (+ stt + budget).

    Reads through the per-role store. The boot path seeds it (migrating a flat
    v1 file forward), so ``read`` never returns ``None`` in the running app; the
    response falls back to the all-default seed regardless.
    """

    store = _role_store_provider()
    selection = store.read()
    if selection is None:
        # Not yet seeded (e.g. a test hitting the route without boot): synthesise
        # the all-default map from .env so the picker always has four roles.
        selection = _role_store_provider().seed_from_settings(_settings_provider())
    return _role_map_payload(selection)


class RoleSelectionUpdateErrorResponse(BaseModel):
    """Structured error body for a failed ``PUT /api/llm/roles/{role}``."""

    error: str
    detail: str


def _role_error(code: str, detail: str, http_status: int) -> JSONResponse:
    """Build a structured ``{error, detail}`` body at ``http_status``."""

    return JSONResponse(
        status_code=http_status,
        content=RoleSelectionUpdateErrorResponse(error=code, detail=detail).model_dump(),
    )


@router.put(
    "/roles/{role}",
    response_model=RoleMapResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": RoleSelectionUpdateErrorResponse},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": RoleSelectionUpdateErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": RoleSelectionUpdateErrorResponse},
    },
)
async def put_llm_role(role: str, body: RoleSelectionBody) -> JSONResponse:
    """Swap ONE role's selection; rebuild ONLY that role's client.

    Validates the role id (must be one of the four) and the provider (``lm_studio``
    | ``claude_cli``) BEFORE any mutation, then delegates to the per-role swap
    coordinator (:class:`bob.llm_swap.RoleLLMSwitcher`), which rebuilds only the
    changed role's client and persists the v2 map. The other three roles' clients
    are untouched.

    Errors → HTTP: unknown role → 404; invalid provider / request → 422; swap not
    wired (lifespan not running) → 503. Returns the full updated role map.
    """

    if role not in ROLES:
        return _role_error(
            "unknown_role",
            f"Unknown LLM role: {role!r}. Expected one of {sorted(ROLES)}.",
            status.HTTP_404_NOT_FOUND,
        )

    if _role_switcher is None:
        return _role_error(
            "swap_unavailable",
            "Per-role LLM swap coordinator not initialised (app lifespan not running)",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    selection = LLMSelection(
        provider=body.provider,
        lm_model=body.lm_model,
        context_length=dict(body.context_length),
        base_url=body.base_url,
        reasoning=body.reasoning,
    )
    try:
        updated = await _role_switcher.swap_role(role, selection)
    except UnknownProviderError as exc:
        return _role_error("unknown_provider", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
    except ModelBudgetExceededError as exc:
        # PRD 0016 / issue 0107, Annexe G "Budget dépassé (check)": the per-host
        # multi-load policy refused this role's model BEFORE loading it because
        # the resident set would exceed the ceiling. 409 (conflict) carries the
        # "dépasse le plafond, libère un rôle" message; the previous role state
        # stands (nothing rebuilt / persisted).
        return _role_error("budget_exceeded", str(exc), status.HTTP_409_CONFLICT)
    except LMStudioModelNotFoundError as exc:
        return _role_error("model_not_found", str(exc), status.HTTP_404_NOT_FOUND)
    except LMStudioLoadError as exc:
        # Annexe G "OOM au load (budget OK mais réel KO)": a real OOM at the SDK
        # despite a passing budget check. The previous state is kept (never 0
        # models for the active role); the role's swap is refused.
        return _role_error("load_failed", str(exc), status.HTTP_409_CONFLICT)
    except LMStudioUnavailableError as exc:
        # Annexe G "Host distant injoignable": the role's host could not be
        # reached during the load policy. The role keeps its previous state; the
        # picker surfaces the host as offline via the ping chip.
        return _role_error("lm_studio_unavailable", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_role_map_payload(updated).model_dump(),
    )


class RoleReasoningUpdateBody(BaseModel):
    """Body for ``PUT /api/llm/roles/{role}/reasoning``.

    ``reasoning`` is one of :data:`REASONING_LEVELS` or ``None`` (omit → the
    model's auto-chosen setting). This is a per-REQUEST chat param, not a
    load-time setting, so updating it never reloads the model or runs the budget
    policy — distinct from the model/provider/url swaps on ``PUT /roles/{role}``.
    """

    reasoning: str | None = None


@router.put(
    "/roles/{role}/reasoning",
    response_model=RoleMapResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": RoleSelectionUpdateErrorResponse},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"model": RoleSelectionUpdateErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": RoleSelectionUpdateErrorResponse},
    },
)
async def put_llm_role_reasoning(role: str, body: RoleReasoningUpdateBody) -> JSONResponse:
    """Update ONLY a role's reasoning level — no model reload, no budget check.

    Reasoning rides on every chat request (LM Studio ``reasoning`` body field),
    so changing it must not rebuild the model load: this delegates to
    :meth:`bob.llm_swap.RoleLLMSwitcher.set_reasoning`, which persists the new
    level and refreshes only the role's cheap client object. Validates the role
    id (404) and the level (422) before any write. Returns the full updated map.
    """

    if role not in ROLES:
        return _role_error(
            "unknown_role",
            f"Unknown LLM role: {role!r}. Expected one of {sorted(ROLES)}.",
            status.HTTP_404_NOT_FOUND,
        )

    if body.reasoning is not None and body.reasoning not in REASONING_LEVELS:
        return _role_error(
            "invalid_reasoning",
            f"Unknown reasoning level: {body.reasoning!r}. "
            f"Expected one of {sorted(REASONING_LEVELS)} or null.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if _role_switcher is None:
        return _role_error(
            "swap_unavailable",
            "Per-role LLM swap coordinator not initialised (app lifespan not running)",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    updated = await _role_switcher.set_reasoning(role, body.reasoning)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_role_map_payload(updated).model_dump(),
    )
