"""Tests for :mod:`bob.lm_studio_manager` and the GET /api/llm/models endpoint.

The ``lmstudio`` SDK is faked at the system boundary (the client factory) so
the suite is fully offline and deterministic — no running LM Studio server is
required. A fake client replays a scripted ``list_downloaded_models`` /
``list_loaded_models`` pair; an "unreachable server" is modelled by a factory
that raises the SDK's ``LMStudioError``.
"""

from __future__ import annotations

from typing import Any, cast

import lmstudio
from fastapi.testclient import TestClient

from bob.config import Settings
from bob.lm_studio_manager import (
    DEFAULT_LM_STUDIO_HOST,
    LMStudioLoadError,
    LMStudioManager,
    LMStudioModelNotFoundError,
    LMStudioUnavailableError,
    _SDKClient,
    _SDKDownloadedModel,
    _SDKLoadedModel,
    _SDKModelInfo,
    host_from_base_url,
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


class _FakeLlmNamespace:
    """Stand-in for the SDK ``client.llm`` session surface.

    Records every ``load_new_instance`` / ``unload`` call so tests can assert
    the validate-then-swap order. ``load_error`` (when set) is raised by
    ``load_new_instance`` to model an OOM / not-found at the boundary.
    """

    def __init__(self, load_error: Exception | None = None) -> None:
        self.load_error = load_error
        self.loaded: list[tuple[str, object]] = []
        self.unloaded: list[str] = []

    def load_new_instance(
        self,
        model_key: str,
        *,
        config: object | None = None,
    ) -> object:
        if self.load_error is not None:
            raise self.load_error
        self.loaded.append((model_key, config))
        return object()

    def unload(self, model_identifier: str) -> None:
        self.unloaded.append(model_identifier)


class _FakeClient:
    """Replays a scripted catalogue; records that close() was called."""

    def __init__(
        self,
        downloaded: list[_SDKDownloadedModel],
        loaded: list[_SDKLoadedModel],
        llm: _FakeLlmNamespace | None = None,
    ) -> None:
        self._downloaded = downloaded
        self._loaded = loaded
        self.closed = False
        self.llm = llm or _FakeLlmNamespace()

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


def test_host_from_base_url_strips_scheme_and_path() -> None:
    # Inference URL (openai client) → bare host:port for the management SDK.
    assert host_from_base_url("http://192.168.86.21:1234/v1") == "192.168.86.21:1234"
    assert host_from_base_url("http://localhost:1234/v1/") == "localhost:1234"
    assert host_from_base_url("192.168.86.21:1234") == "192.168.86.21:1234"


def test_host_from_base_url_falls_back_when_absent() -> None:
    assert host_from_base_url(None) == DEFAULT_LM_STUDIO_HOST
    assert host_from_base_url("") == DEFAULT_LM_STUDIO_HOST


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


def test_load_loads_target_and_unloads_previous() -> None:
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("old-model")],
        llm=llm,
    )

    def factory(_host: str) -> _SDKClient:
        return client

    manager = LMStudioManager(host="localhost:1234", client_factory=factory)

    manager.load("qwen2.5-7b-instruct", context_length=8192)

    # Loaded the target with the default ctx folded into the SDK config.
    assert llm.loaded == [("qwen2.5-7b-instruct", {"contextLength": 8192})]
    # Previously-loaded model evicted BEFORE the load (offload-first frees VRAM).
    assert llm.unloaded == ["old-model"]
    assert client.closed is True


def test_load_already_loaded_plain_select_is_noop() -> None:
    # The target is already resident and this is a plain select (no reload):
    # keep it loaded, offload nothing, load nothing ("just select it").
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("qwen2.5-7b-instruct"), _FakeLoaded("other-model")],
        llm=llm,
    )

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    manager.load("qwen2.5-7b-instruct")

    assert llm.loaded == []  # no reload
    assert llm.unloaded == []  # the resident target (and peers) left untouched


def test_load_already_loaded_with_reload_evicts_all_and_reloads() -> None:
    # Forced reload (ctx Apply): the resident target IS evicted then reloaded at
    # the new window, and other residents are freed too.
    llm = _FakeLlmNamespace()
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("qwen2.5-7b-instruct"), _FakeLoaded("other-model")],
        llm=llm,
    )

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    manager.load("qwen2.5-7b-instruct", context_length=4096, reload=True)

    assert llm.loaded == [("qwen2.5-7b-instruct", {"contextLength": 4096})]
    assert set(llm.unloaded) == {"qwen2.5-7b-instruct", "other-model"}


def test_load_without_context_length_omits_config() -> None:
    llm = _FakeLlmNamespace()
    client = _FakeClient(_catalogue(), loaded=[], llm=llm)

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    manager.load("qwen2.5-7b-instruct")

    assert llm.loaded == [("qwen2.5-7b-instruct", None)]
    assert llm.unloaded == []


def test_load_unknown_model_raises_not_found_and_keeps_previous() -> None:
    llm = _FakeLlmNamespace(load_error=lmstudio.LMStudioModelNotFoundError("no such model"))
    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("old-model")], llm=llm)

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)

    try:
        manager.load("ghost-model")
    except LMStudioModelNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LMStudioModelNotFoundError")

    # Offload-first: the previous model is evicted BEFORE the (failing) load, so
    # a failed load leaves no model resident (accepted tradeoff to kill OOM).
    assert llm.unloaded == ["old-model"]


def test_load_failure_raises_load_error_and_keeps_previous() -> None:
    llm = _FakeLlmNamespace(load_error=lmstudio.LMStudioServerError("out of memory"))
    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("old-model")], llm=llm)

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)

    try:
        manager.load("qwen2.5-7b-instruct")
    except LMStudioLoadError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LMStudioLoadError")

    # Offload-first: previous evicted before the failing load.
    assert llm.unloaded == ["old-model"]


def test_loaded_model_ids_returns_identifiers() -> None:
    client = _FakeClient(
        _catalogue(),
        loaded=[_FakeLoaded("a"), _FakeLoaded("b")],
    )

    manager = LMStudioManager(host="h", client_factory=lambda _h: client)
    assert manager.loaded_model_ids() == ["a", "b"]


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


# --- GET /api/llm/ping endpoint tests ---------------------------------------


def test_ping_endpoint_reachable_server_returns_true() -> None:
    from bob import llm_router

    client = _FakeClient(_catalogue(), loaded=[_FakeLoaded("qwen2.5-7b-instruct")])
    manager = LMStudioManager(host="localhost:1234", client_factory=lambda _h: client)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/ping")
    finally:
        llm_router.reset_manager_provider()

    assert response.status_code == 200
    assert response.json() == {"reachable": True, "host": "localhost:1234"}


def test_ping_endpoint_unreachable_server_returns_false_not_error() -> None:
    from bob import llm_router

    def _boom(_host: str) -> _SDKClient:
        raise lmstudio.LMStudioWebsocketError("connection refused")

    manager = LMStudioManager(host="localhost:1234", client_factory=_boom)

    llm_router.set_manager_provider(lambda: manager)
    try:
        api = TestClient(app)
        response = api.get("/api/llm/ping")
    finally:
        llm_router.reset_manager_provider()

    # Always 200 — the picker reads `reachable`, never an error status.
    assert response.status_code == 200
    assert response.json() == {"reachable": False, "host": "localhost:1234"}


# --- PUT /api/llm/selection endpoint tests ----------------------------------
#
# The route delegates to a :class:`bob.llm_swap.LLMSwitcher`. We inject a fake
# switcher through the router DI seam so the test exercises the route's HTTP
# contract (status mapping, body shape) without the orchestrator / SDK. The
# swap coordinator's own behaviour is covered in ``test_llm_swap.py``.


class _FakeSwitcher:
    """Stand-in for :class:`LLMSwitcher` — returns a result or raises."""

    def __init__(self, *, result: object = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, int | None]] = []

    async def swap_lm_model(
        self, model_id: str, context_length: int | None = None
    ) -> object:
        self.calls.append((model_id, context_length))
        if self._error is not None:
            raise self._error
        return self._result


def test_put_selection_success_returns_new_selection() -> None:
    from bob import llm_router
    from bob.llm_selection_store import LLMSelection
    from bob.llm_swap import SwapResult

    selection = LLMSelection(
        provider="lm_studio",
        lm_model="target-model",
        context_length={"target-model": 8192},
    )
    switcher = _FakeSwitcher(result=SwapResult(selection=selection))

    llm_router.set_switcher(cast(Any, switcher))
    llm_router.set_settings_provider(lambda: _settings(CLAUDE_CLI_MODEL="claude-opus-4"))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "target-model"})
    finally:
        llm_router.set_switcher(None)
        llm_router.reset_settings_provider()

    assert response.status_code == 200
    assert switcher.calls == [("target-model", None)]
    body = response.json()
    # ``claude_model`` (issue 0081) is now part of the response shape; the
    # model-swap fields are unchanged.
    assert body == {
        "provider": "lm_studio",
        "lm_model": "target-model",
        "context_length": {"target-model": 8192},
        "claude_model": "claude-opus-4",
        # No pinned base_url on the swap result → falls back to LLM_BASE_URL.
        "base_url": "http://localhost:1234/v1",
    }


def test_put_selection_not_found_maps_to_404() -> None:
    from bob import llm_router

    switcher = _FakeSwitcher(error=LMStudioModelNotFoundError("ghost"))
    llm_router.set_switcher(cast(Any, switcher))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "ghost"})
    finally:
        llm_router.set_switcher(None)

    assert response.status_code == 404
    assert response.json()["error"] == "model_not_found"


def test_put_selection_load_failure_maps_to_409() -> None:
    from bob import llm_router

    switcher = _FakeSwitcher(error=LMStudioLoadError("out of memory"))
    llm_router.set_switcher(cast(Any, switcher))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "big-model"})
    finally:
        llm_router.set_switcher(None)

    assert response.status_code == 409
    assert response.json()["error"] == "load_failed"


def test_put_selection_unreachable_maps_to_503() -> None:
    from bob import llm_router

    switcher = _FakeSwitcher(error=LMStudioUnavailableError("server down"))
    llm_router.set_switcher(cast(Any, switcher))
    try:
        api = TestClient(app)
        response = api.put("/api/llm/selection", json={"lm_model": "m"})
    finally:
        llm_router.set_switcher(None)

    assert response.status_code == 503
    assert response.json()["error"] == "lm_studio_unavailable"


def test_put_selection_no_switcher_returns_503() -> None:
    api = TestClient(app)
    response = api.put("/api/llm/selection", json={"lm_model": "m"})
    assert response.status_code == 503
    assert response.json()["error"] == "swap_unavailable"
