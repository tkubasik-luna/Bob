"""Tests for :mod:`bob.llm_selection_store` and the GET selection endpoint."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from bob.config import Settings
from bob.llm_selection_store import (
    LLM_SELECTION_FILENAME,
    LLMSelection,
    LLMSelectionStore,
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


def test_first_boot_seeds_from_settings_and_persists(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    store = LLMSelectionStore(path)
    assert store.read() is None  # nothing persisted yet

    seeded = store.seed_from_settings(_settings())

    assert seeded.provider == "lm_studio"
    assert seeded.lm_model == "qwen2.5-7b-instruct"
    assert seeded.context_length == {}

    # The JSON file now exists with the seeded shape.
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {
        "provider": "lm_studio",
        "lm_model": "qwen2.5-7b-instruct",
        "context_length": {},
    }


def test_json_wins_over_env_on_later_boots(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    # Pre-existing JSON differs from what .env would seed.
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
    store = LLMSelectionStore(path)

    # .env says lm_studio / qwen but the JSON must win.
    selection = store.seed_from_settings(_settings(LLM_PROVIDER="lm_studio"))

    assert selection.provider == "claude_cli"
    assert selection.lm_model == "persisted-model"
    assert selection.context_length == {"persisted-model": 200000}


def test_context_length_map_round_trips(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    store = LLMSelectionStore(path)
    store.write(
        LLMSelection(
            provider="lm_studio",
            lm_model="model-a",
            context_length={"model-a": 32768, "model-b": 8192},
        )
    )

    reloaded = LLMSelectionStore(path).read()

    assert reloaded is not None
    assert reloaded.provider == "lm_studio"
    assert reloaded.lm_model == "model-a"
    assert reloaded.context_length == {"model-a": 32768, "model-b": 8192}


def test_read_decodes_corrupt_file_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / LLM_SELECTION_FILENAME
    path.write_text("{ not valid json", encoding="utf-8")

    selection = LLMSelectionStore(path).read()

    assert selection is not None
    assert selection.provider == "lm_studio"
    assert selection.lm_model is None
    assert selection.context_length == {}


def test_get_endpoint_returns_current_selection(tmp_path: Path) -> None:
    from bob import llm_router

    path = tmp_path / LLM_SELECTION_FILENAME
    store = LLMSelectionStore(path)
    store.write(
        LLMSelection(
            provider="lm_studio",
            lm_model="endpoint-model",
            context_length={"endpoint-model": 16384},
        )
    )

    llm_router.set_store_provider(lambda: store)
    try:
        # TestClient without the ``with`` block does not run the lifespan, so
        # the DI-seam store above is what the route reads.
        client = TestClient(app)
        response = client.get("/api/llm/selection")
    finally:
        llm_router.reset_store_provider()

    assert response.status_code == 200
    assert response.json() == {
        "provider": "lm_studio",
        "lm_model": "endpoint-model",
        "context_length": {"endpoint-model": 16384},
    }
