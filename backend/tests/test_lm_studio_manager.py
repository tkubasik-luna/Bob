"""Tests for :mod:`bob.lm_studio_manager` and the GET /api/llm/models endpoint.

The ``lmstudio`` SDK is faked at the system boundary (the client factory) so
the suite is fully offline and deterministic — no running LM Studio server is
required. A fake client replays a scripted ``list_downloaded_models`` /
``list_loaded_models`` pair; an "unreachable server" is modelled by a factory
that raises the SDK's ``LMStudioError``.
"""

from __future__ import annotations

import lmstudio
from fastapi.testclient import TestClient

from bob.lm_studio_manager import (
    LMStudioManager,
    LMStudioUnavailableError,
    _SDKClient,
    _SDKDownloadedModel,
    _SDKLoadedModel,
    _SDKModelInfo,
)
from bob.main import app

# --- SDK fakes ---------------------------------------------------------------


class _FakeInfo:
    """Stand-in for the SDK ``LlmInfo`` / ``EmbeddingModelInfo`` struct."""

    def __init__(
        self,
        *,
        type: str,
        model_key: str,
        format: str | None = "Q4_K_M",
        architecture: str | None = "qwen2",
        max_context_length: int | None = 32768,
        vision: bool = False,
    ) -> None:
        self.type = type
        self.model_key = model_key
        self.format = format
        self.architecture = architecture
        self.max_context_length = max_context_length
        self.vision = vision


class _FakeDownloaded:
    def __init__(self, info: _SDKModelInfo) -> None:
        self.info = info


class _FakeLoaded:
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier


class _FakeClient:
    """Replays a scripted catalogue; records that close() was called."""

    def __init__(
        self,
        downloaded: list[_SDKDownloadedModel],
        loaded: list[_SDKLoadedModel],
    ) -> None:
        self._downloaded = downloaded
        self._loaded = loaded
        self.closed = False

    def list_downloaded_models(self) -> list[_SDKDownloadedModel]:
        return list(self._downloaded)

    def list_loaded_models(self) -> list[_SDKLoadedModel]:
        return list(self._loaded)

    def close(self) -> None:
        self.closed = True


def _catalogue() -> list[_SDKDownloadedModel]:
    return [
        _FakeDownloaded(
            _FakeInfo(type="llm", model_key="qwen2.5-7b-instruct", max_context_length=32768)
        ),
        _FakeDownloaded(
            _FakeInfo(
                type="vlm",
                model_key="qwen2-vl-7b",
                format="Q5_K_M",
                architecture="qwen2_vl",
                max_context_length=8192,
                vision=True,
            )
        ),
        # An embedding model — MUST be filtered out.
        _FakeDownloaded(
            _FakeInfo(
                type="embedding",
                model_key="nomic-embed-text",
                architecture="nomic-bert",
                max_context_length=2048,
            )
        ),
    ]


# --- LMStudioManager unit tests ---------------------------------------------


def test_list_models_filters_embeddings_and_exposes_metadata() -> None:
    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("qwen2.5-7b-instruct")])

    def factory(_host: str) -> _SDKClient:
        return client

    manager = LMStudioManager(host="localhost:1234", client_factory=factory)

    models = manager.list_models()

    ids = [m.id for m in models]
    assert ids == ["qwen2.5-7b-instruct", "qwen2-vl-7b"]  # embedding excluded
    assert "nomic-embed-text" not in ids

    chat = models[0]
    assert chat.id == "qwen2.5-7b-instruct"
    assert chat.quantisation == "Q4_K_M"
    assert chat.architecture == "qwen2"
    assert chat.max_context_length == 32768
    assert chat.loaded is True  # present in list_loaded_models

    vlm = models[1]
    assert vlm.quantisation == "Q5_K_M"
    assert vlm.architecture == "qwen2_vl"
    assert vlm.max_context_length == 8192
    assert vlm.loaded is False  # not loaded

    assert client.closed is True  # manager closes the client


def test_list_models_unreachable_server_raises_distinct_error() -> None:
    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    manager = LMStudioManager(host="localhost:1234", client_factory=_boom)

    try:
        manager.list_models()
    except LMStudioUnavailableError as exc:
        assert "localhost:1234" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected LMStudioUnavailableError")


# --- GET /api/llm/models endpoint tests -------------------------------------


def test_get_models_endpoint_returns_live_list() -> None:
    from bob import llm_router

    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("qwen2.5-7b-instruct")])

    def factory(_host: str) -> _SDKClient:
        return client

    manager = LMStudioManager(host="localhost:1234", client_factory=factory)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/models")
    finally:
        llm_router.reset_manager_provider()

    assert response.status_code == 200
    body = response.json()
    ids = [m["id"] for m in body["models"]]
    assert ids == ["qwen2.5-7b-instruct", "qwen2-vl-7b"]
    assert "nomic-embed-text" not in ids
    first = body["models"][0]
    assert first == {
        "id": "qwen2.5-7b-instruct",
        "quantisation": "Q4_K_M",
        "architecture": "qwen2",
        "max_context_length": 32768,
        "loaded": True,
    }


def test_get_models_endpoint_server_down_returns_503() -> None:
    from bob import llm_router

    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    manager = LMStudioManager(host="localhost:1234", client_factory=_boom)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/models")
    finally:
        llm_router.reset_manager_provider()

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "lm_studio_unavailable"
    assert "localhost:1234" in body["detail"]
