"""Read-only REST endpoints for the LLM selection (PRD 0012 / issue 0078).

Exposes ``GET /api/llm/selection`` returning the current selection owned by
:class:`bob.llm_selection_store.LLMSelectionStore`. This slice is read-only —
no mutation endpoint, no model loading, no client rebuild.

The store is resolved through a DI seam (mirroring :mod:`bob.debug_router`) so
tests can prime their own store without running the full app lifespan.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter
from pydantic import BaseModel

from bob.llm_selection_store import (
    LLMSelectionStore,
    get_default_store,
)

router = APIRouter(prefix="/api/llm", tags=["llm"])

# DI seam so the route test can swap the store factory without booting the
# whole app. Defaults to the process-wide singleton primed by the lifespan.
_store_provider: Callable[[], LLMSelectionStore] = get_default_store


def set_store_provider(provider: Callable[[], LLMSelectionStore]) -> None:
    """Override the selection-store factory used by the endpoints."""

    global _store_provider
    _store_provider = provider


def reset_store_provider() -> None:
    """Restore the default selection-store factory (the singleton)."""

    global _store_provider
    _store_provider = get_default_store


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
