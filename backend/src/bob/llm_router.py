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

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bob.config import Settings, get_settings
from bob.llm_selection_store import (
    LLMSelectionStore,
    get_default_store,
)
from bob.llm_swap import (
    ClaudeCliUnavailableError,
    LLMSwitcher,
    UnknownProviderError,
)
from bob.lm_studio_manager import (
    LMStudioLoadError,
    LMStudioManager,
    LMStudioModelNotFoundError,
    LMStudioUnavailableError,
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
    claude_model = _settings_provider().CLAUDE_CLI_MODEL or DEFAULT_CLAUDE_MODEL_LABEL
    return LLMSelectionResponse(
        provider=provider,
        lm_model=lm_model,
        context_length=context_length,
        claude_model=claude_model,
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
def get_llm_models() -> JSONResponse:
    """Return the live list of chat-capable LM Studio models.

    Embedding models are excluded by the manager. When the LM Studio server is
    unreachable, returns HTTP 503 with a DISTINCT, structured error body rather
    than letting the SDK error bubble into a 500 traceback — the frontend can
    show "serveur LM Studio injoignable" and the picker degrades gracefully.
    """

    manager = _manager_provider()
    try:
        models = manager.list_models()
    except LMStudioUnavailableError as exc:
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
    claude_model = _settings_provider().CLAUDE_CLI_MODEL or DEFAULT_CLAUDE_MODEL_LABEL
    payload = LLMSelectionResponse(
        provider=selection.provider,  # type: ignore[attr-defined]
        lm_model=selection.lm_model,  # type: ignore[attr-defined]
        context_length=selection.context_length,  # type: ignore[attr-defined]
        claude_model=claude_model,
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
    if has_model == has_provider:
        return _error_response(
            "invalid_request",
            "Exactly one of 'lm_model' or 'provider' must be provided",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if has_provider and body.context_length is not None:
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
        return _error_response(
            "unknown_provider", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY
        )
    except ClaudeCliUnavailableError as exc:
        return _error_response(
            "claude_cli_unavailable", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE
        )
    except LMStudioUnavailableError as exc:
        return _error_response(
            "lm_studio_unavailable", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return _selection_payload(result.selection)
