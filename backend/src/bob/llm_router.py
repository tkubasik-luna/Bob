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

from bob.llm_selection_store import (
    LLMSelectionStore,
    get_default_store,
)
from bob.lm_studio_manager import LMStudioManager, LMStudioUnavailableError

router = APIRouter(prefix="/api/llm", tags=["llm"])

# DI seam so the route test can swap the store factory without booting the
# whole app. Defaults to the process-wide singleton primed by the lifespan.
_store_provider: Callable[[], LLMSelectionStore] = get_default_store

# DI seam for the LM Studio management client. Defaults to a fresh manager
# pointing at the local server; tests inject one wired to a fake SDK client.
_manager_provider: Callable[[], LMStudioManager] = LMStudioManager


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


class LLMSelectionResponse(BaseModel):
    """Body for ``GET /api/llm/selection``."""

    provider: str
    lm_model: str | None
    context_length: dict[str, int]


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
    return LLMSelectionResponse(
        provider=provider,
        lm_model=lm_model,
        context_length=context_length,
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
