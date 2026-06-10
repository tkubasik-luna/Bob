"""Tests for the GET /api/llm/selection flat view over the per-role store.

The flat v1 ``LLMSelectionStore`` is gone: :class:`RoleSelectionStore` is the
single owner of ``llm_selection.json`` and the global ``/selection`` surface is
a VIEW over its ``jarvis`` role. These tests pin that projection (and the
related clobber regressions); the store's own decode/migration behaviour is
covered by ``test_role_selection_store.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from bob.config import Settings
from bob.llm_selection_store import (
    LLM_SELECTION_FILENAME,
    ROLES,
    LLMSelection,
    RoleSelection,
    RoleSelectionStore,
)
from bob.main import app


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LLM_PROVIDER": "lm_studio",
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_MODEL": "qwen2.5-7b-instruct",
        "LLM_API_KEY": "lm-studio",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _store_with_jarvis(path: Path, jarvis: LLMSelection) -> RoleSelectionStore:
    store = RoleSelectionStore(path)
    store.write(RoleSelection(roles={role: jarvis for role in ROLES}))
    return store


def test_get_endpoint_returns_jarvis_view(tmp_path: Path) -> None:
    from bob import llm_router

    path = tmp_path / LLM_SELECTION_FILENAME
    store = _store_with_jarvis(
        path,
        LLMSelection(
            provider="lm_studio",
            lm_model="endpoint-model",
            context_length={"endpoint-model": 16384},
        ),
    )

    llm_router.set_role_store_provider(lambda: store)
    llm_router.set_settings_provider(lambda: _settings(CLAUDE_CLI_MODEL="claude-opus-4"))
    try:
        # TestClient without the ``with`` block does not run the lifespan, so
        # the DI-seam store above is what the route reads.
        client = TestClient(app)
        response = client.get("/api/llm/selection")
    finally:
        llm_router.reset_role_store_provider()
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    # ``claude_model`` (issue 0081) is the read-only Claude label the picker
    # shows on the Claude side — surfaced from ``CLAUDE_CLI_MODEL``.
    assert response.json() == {
        "provider": "lm_studio",
        "lm_model": "endpoint-model",
        "context_length": {"endpoint-model": 16384},
        "claude_model": "claude-opus-4",
        # base_url falls back to the active LLM_BASE_URL when the selection
        # pins none (so the picker shows the server actually loaded).
        "base_url": "http://localhost:1234/v1",
    }


def test_get_endpoint_projects_jarvis_not_other_roles(tmp_path: Path) -> None:
    """The flat view is the JARVIS role — a differently-pinned thinker must not leak."""

    from bob import llm_router

    path = tmp_path / LLM_SELECTION_FILENAME
    store = RoleSelectionStore(path)
    jarvis = LLMSelection(
        provider="lm_studio", lm_model="jarvis-model", base_url="http://jarvis:1234/v1"
    )
    thinker = LLMSelection(
        provider="claude_cli", lm_model="thinker-model", base_url="http://thinker:1234/v1"
    )
    store.write(
        RoleSelection(
            roles={"jarvis": jarvis, "thinker": thinker, "draft": jarvis, "subagent": jarvis}
        )
    )

    llm_router.set_role_store_provider(lambda: store)
    llm_router.set_settings_provider(lambda: _settings())
    try:
        response = TestClient(app).get("/api/llm/selection")
    finally:
        llm_router.reset_role_store_provider()
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    body = response.json()
    assert body["lm_model"] == "jarvis-model"
    assert body["provider"] == "lm_studio"
    assert body["base_url"] == "http://jarvis:1234/v1"


def test_get_endpoint_reads_flat_v1_file_via_migration(tmp_path: Path) -> None:
    """A legacy flat v1 file still serves the GET (role decode migrates it)."""

    from bob import llm_router

    path = tmp_path / LLM_SELECTION_FILENAME
    path.write_text(
        json.dumps(
            {
                "provider": "claude_cli",
                "lm_model": "persisted-model",
                "context_length": {"persisted-model": 200000},
            }
        ),
        encoding="utf-8",
    )
    store = RoleSelectionStore(path)

    llm_router.set_role_store_provider(lambda: store)
    llm_router.set_settings_provider(lambda: _settings())
    try:
        response = TestClient(app).get("/api/llm/selection")
    finally:
        llm_router.reset_role_store_provider()
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "claude_cli"
    assert body["lm_model"] == "persisted-model"
    assert body["context_length"] == {"persisted-model": 200000}


def test_get_endpoint_claude_model_falls_back_to_default(tmp_path: Path) -> None:
    """With ``CLAUDE_CLI_MODEL`` unset, the picker still gets a non-empty label."""

    from bob import llm_router

    path = tmp_path / LLM_SELECTION_FILENAME
    store = _store_with_jarvis(
        path, LLMSelection(provider="lm_studio", lm_model="m", context_length={})
    )

    llm_router.set_role_store_provider(lambda: store)
    llm_router.set_settings_provider(lambda: _settings(CLAUDE_CLI_MODEL=None))
    try:
        client = TestClient(app)
        response = client.get("/api/llm/selection")
    finally:
        llm_router.reset_role_store_provider()
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    assert response.json()["claude_model"] == llm_router.DEFAULT_CLAUDE_MODEL_LABEL
