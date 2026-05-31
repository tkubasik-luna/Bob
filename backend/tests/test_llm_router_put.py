"""HTTP-level tests for ``PUT /api/llm/selection`` (PRD 0012 / issues 0080-0081).

The route is a thin shell over :class:`bob.llm_swap.LLMSwitcher`; here we inject
a FAKE switcher through the router's ``set_switcher`` DI seam so the test never
touches the ``lmstudio`` SDK or the ``claude`` binary. We assert the route's own
contract: body validation (exactly one mutation), dispatch to the right swap
method, and the error-type → HTTP-status mapping for the provider branch.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from bob import llm_router
from bob.config import Settings
from bob.llm_selection_store import LLMSelection
from bob.llm_swap import (
    ClaudeCliUnavailableError,
    SwapResult,
    UnknownProviderError,
)
from bob.lm_studio_manager import LMStudioUnavailableError
from bob.main import app


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LLM_PROVIDER": "lm_studio",
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_MODEL": "qwen2.5-7b-instruct",
        "LLM_API_KEY": "lm-studio",
        "CLAUDE_CLI_MODEL": "claude-opus-4",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeSwitcher:
    """Records the swap call and returns / raises a scripted outcome."""

    def __init__(
        self,
        *,
        provider_result: SwapResult | None = None,
        provider_error: Exception | None = None,
    ) -> None:
        self.provider_result = provider_result
        self.provider_error = provider_error
        self.provider_calls: list[str] = []
        self.model_calls: list[tuple[str, int | None]] = []

    async def swap_provider(self, provider: str) -> SwapResult:
        self.provider_calls.append(provider)
        if self.provider_error is not None:
            raise self.provider_error
        assert self.provider_result is not None
        return self.provider_result

    async def swap_lm_model(
        self, model_id: str, context_length: int | None = None
    ) -> SwapResult:
        self.model_calls.append((model_id, context_length))
        assert self.provider_result is not None
        return self.provider_result


def _client(switcher: _FakeSwitcher | None) -> TestClient:
    llm_router.set_switcher(switcher)  # type: ignore[arg-type]
    llm_router.set_settings_provider(lambda: _settings())
    return TestClient(app)


def _reset() -> None:
    llm_router.set_switcher(None)
    llm_router.reset_settings_provider()


def test_put_provider_success_returns_new_selection_with_claude_label() -> None:
    switcher = _FakeSwitcher(
        provider_result=SwapResult(
            selection=LLMSelection(
                provider="claude_cli", lm_model="boot-model", context_length={}
            )
        )
    )
    client = _client(switcher)
    try:
        response = client.put("/api/llm/selection", json={"provider": "claude_cli"})
    finally:
        _reset()

    assert response.status_code == 200
    assert switcher.provider_calls == ["claude_cli"]
    assert switcher.model_calls == []
    body = response.json()
    assert body["provider"] == "claude_cli"
    assert body["claude_model"] == "claude-opus-4"


def test_put_provider_claude_missing_binary_maps_to_503() -> None:
    switcher = _FakeSwitcher(provider_error=ClaudeCliUnavailableError("no claude"))
    client = _client(switcher)
    try:
        response = client.put("/api/llm/selection", json={"provider": "claude_cli"})
    finally:
        _reset()

    assert response.status_code == 503
    assert response.json()["error"] == "claude_cli_unavailable"


def test_put_provider_lm_studio_unreachable_maps_to_503() -> None:
    switcher = _FakeSwitcher(provider_error=LMStudioUnavailableError("down"))
    client = _client(switcher)
    try:
        response = client.put("/api/llm/selection", json={"provider": "lm_studio"})
    finally:
        _reset()

    assert response.status_code == 503
    assert response.json()["error"] == "lm_studio_unavailable"


def test_put_unknown_provider_maps_to_422() -> None:
    switcher = _FakeSwitcher(provider_error=UnknownProviderError("nope"))
    client = _client(switcher)
    try:
        response = client.put("/api/llm/selection", json={"provider": "gpt5"})
    finally:
        _reset()

    assert response.status_code == 422
    assert response.json()["error"] == "unknown_provider"


def test_put_rejects_body_with_both_fields() -> None:
    switcher = _FakeSwitcher(
        provider_result=SwapResult(
            selection=LLMSelection(provider="lm_studio", lm_model="m", context_length={})
        )
    )
    client = _client(switcher)
    try:
        response = client.put(
            "/api/llm/selection", json={"provider": "lm_studio", "lm_model": "m"}
        )
    finally:
        _reset()

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    # Neither swap path was taken.
    assert switcher.provider_calls == []
    assert switcher.model_calls == []


def test_put_rejects_empty_body() -> None:
    switcher = _FakeSwitcher()
    client = _client(switcher)
    try:
        response = client.put("/api/llm/selection", json={})
    finally:
        _reset()

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_put_model_with_context_length_threads_ctx_to_switcher() -> None:
    switcher = _FakeSwitcher(
        provider_result=SwapResult(
            selection=LLMSelection(
                provider="lm_studio", lm_model="m", context_length={"m": 32768}
            )
        )
    )
    client = _client(switcher)
    try:
        response = client.put(
            "/api/llm/selection", json={"lm_model": "m", "context_length": 32768}
        )
    finally:
        _reset()

    assert response.status_code == 200
    assert switcher.model_calls == [("m", 32768)]
    assert switcher.provider_calls == []


def test_put_model_without_context_length_passes_none() -> None:
    switcher = _FakeSwitcher(
        provider_result=SwapResult(
            selection=LLMSelection(provider="lm_studio", lm_model="m", context_length={})
        )
    )
    client = _client(switcher)
    try:
        response = client.put("/api/llm/selection", json={"lm_model": "m"})
    finally:
        _reset()

    assert response.status_code == 200
    assert switcher.model_calls == [("m", None)]


def test_put_provider_with_context_length_rejected_422() -> None:
    switcher = _FakeSwitcher(
        provider_result=SwapResult(
            selection=LLMSelection(provider="lm_studio", lm_model=None, context_length={})
        )
    )
    client = _client(switcher)
    try:
        response = client.put(
            "/api/llm/selection", json={"provider": "lm_studio", "context_length": 8192}
        )
    finally:
        _reset()

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert switcher.provider_calls == []
    assert switcher.model_calls == []


def test_put_non_positive_context_length_rejected_422() -> None:
    switcher = _FakeSwitcher(
        provider_result=SwapResult(
            selection=LLMSelection(provider="lm_studio", lm_model="m", context_length={})
        )
    )
    client = _client(switcher)
    try:
        response = client.put(
            "/api/llm/selection", json={"lm_model": "m", "context_length": 0}
        )
    finally:
        _reset()

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert switcher.model_calls == []


def test_put_provider_503_when_switcher_not_wired() -> None:
    client = _client(None)
    try:
        response = client.put("/api/llm/selection", json={"provider": "lm_studio"})
    finally:
        _reset()

    assert response.status_code == 503
    assert response.json()["error"] == "swap_unavailable"
