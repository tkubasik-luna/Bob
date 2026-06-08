"""Tests for the LM Studio SDK chat client + transport-flag selection (PRD 0017).

The ``lmstudio`` SDK is faked at its boundary — a fake ``AsyncClient`` factory
injected into :class:`bob.llm.lmstudio_sdk.LMStudioSDKClient` — so the suite is
fully offline and deterministic (model: ``test_lm_studio_manager``). No running
LM Studio server is required.

Coverage:
- flag selection: the factory returns the SDK client under ``sdk`` and the
  OpenAI client under ``openai``, for BOTH the global and the per-role path;
- ``chat()`` surface parity vs :class:`LMStudioClient` (same input → same
  returned string);
- the structured-output schema mapping passed to ``respond(response_format=…)``;
- empty content → :class:`LLMClientError`;
- an SDK ``LMStudioError`` from ``respond`` → :class:`LLMClientError`;
- the per-role ``reasoning`` level riding on ``config.raw``.
"""

from __future__ import annotations

from typing import Any

import lmstudio
import pytest

from bob.config import Settings
from bob.llm.factory import _build_for_backend, _build_role_client
from bob.llm.lmstudio_sdk import LMStudioSDKClient
from bob.llm.lmstudio_sdk.client import schema_to_response_format
from bob.llm_client import LLMClient, LLMClientError, LMStudioClient
from bob.llm_selection_store import LLMSelection, RoleSelection


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


class _FakeStats:
    def __init__(self, prompt: int | None, predicted: int | None) -> None:
        self.prompt_tokens_count = prompt
        self.predicted_tokens_count = predicted


class _FakeResult:
    """Stand-in for the SDK ``PredictionResult``."""

    def __init__(
        self,
        content: str = "",
        *,
        parsed: object | None = None,
        prompt_tokens: int | None = 11,
        predicted_tokens: int | None = 7,
    ) -> None:
        self.content = content
        self.parsed = parsed
        self.stats = _FakeStats(prompt_tokens, predicted_tokens)


class _FakeModel:
    """Stand-in for the SDK ``AsyncLLM`` handle. Records the ``respond`` call."""

    def __init__(self, result: _FakeResult | None, error: Exception | None) -> None:
        self._result = result
        self._error = error
        self.respond_calls: list[dict[str, Any]] = []

    async def respond(
        self,
        history: Any,
        *,
        response_format: Any = None,
        config: Any = None,
    ) -> _FakeResult:
        self.respond_calls.append(
            {"history": history, "response_format": response_format, "config": config}
        )
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _FakeLlmNamespace:
    def __init__(self, model: _FakeModel) -> None:
        self._model = model
        self.model_keys: list[str | None] = []

    async def model(self, model_key: str | None = None) -> _FakeModel:
        self.model_keys.append(model_key)
        return self._model


class _FakeAsyncClient:
    """Stand-in for the SDK ``AsyncClient`` (async context manager)."""

    def __init__(self, model: _FakeModel) -> None:
        self.llm = _FakeLlmNamespace(model)
        self.closed = False

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


def _factory_for(
    captured: dict[str, Any],
    result: _FakeResult | None = None,
    error: Exception | None = None,
) -> Any:
    """Build a fake-client factory; records the built client + host in ``captured``."""

    def factory(host: str) -> Any:
        model = _FakeModel(result if result is not None else _FakeResult("ok"), error)
        client = _FakeAsyncClient(model)
        captured["host"] = host
        captured["client"] = client
        captured["model"] = model
        return client

    return factory


# --- schema mapping ----------------------------------------------------------


def test_schema_to_response_format_unwraps_inner_schema() -> None:
    schema = {"name": "snap", "schema": {"type": "object", "properties": {}}}
    assert schema_to_response_format(schema) == {
        "type": "json",
        "jsonSchema": {"type": "object", "properties": {}},
    }


def test_schema_to_response_format_bare_schema() -> None:
    schema = {"type": "object"}
    assert schema_to_response_format(schema) == {
        "type": "json",
        "jsonSchema": {"type": "object"},
    }


# --- flag selection ----------------------------------------------------------


def test_factory_global_openai_default() -> None:
    client = _build_for_backend("lm_studio", _settings())
    assert isinstance(client, LMStudioClient)
    assert not isinstance(client, LMStudioSDKClient)


def test_factory_global_sdk() -> None:
    client = _build_for_backend("lm_studio", _settings(LLM_LMSTUDIO_TRANSPORT="sdk"))
    assert isinstance(client, LMStudioSDKClient)


def _role_selection(reasoning: str | None = None) -> RoleSelection:
    sel = LLMSelection(
        provider="lm_studio",
        lm_model="role-model",
        base_url="http://localhost:1234/v1",
        reasoning=reasoning,
    )
    return RoleSelection(roles=dict.fromkeys(("jarvis", "thinker", "draft", "subagent"), sel))


def test_factory_role_openai_default() -> None:
    client = _build_role_client(_role_selection(), "jarvis", _settings())
    assert isinstance(client, LMStudioClient)
    assert not isinstance(client, LMStudioSDKClient)


def test_factory_role_sdk_passes_model_and_reasoning() -> None:
    settings = _settings(LLM_LMSTUDIO_TRANSPORT="sdk")
    client = _build_role_client(_role_selection(reasoning="high"), "jarvis", settings)
    assert isinstance(client, LMStudioSDKClient)
    # The role's model + reasoning must be threaded through to the SDK client.
    assert client._model == "role-model"
    assert client._reasoning == "high"


# --- chat() behaviour --------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_returns_content() -> None:
    captured: dict[str, Any] = {}
    client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        client_factory=_factory_for(captured, _FakeResult("hello world")),
    )
    out = await client.chat([{"role": "user", "content": "hi"}])
    assert out == "hello world"
    # Host derived from LLM_BASE_URL; model handle resolved from the pinned model.
    assert captured["host"] == "localhost:1234"
    assert captured["model"].respond_calls[0]["response_format"] is None
    assert captured["client"].closed is True


@pytest.mark.asyncio
async def test_chat_surface_parity_with_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same input → same returned string across both transports.

    The OpenAI client's HTTP call is faked to return the same content the SDK
    fake returns, proving observable equivalence of ``chat()``'s return value.
    """

    messages = [{"role": "user", "content": "ping"}]
    content = "pong"

    # SDK transport.
    captured: dict[str, Any] = {}
    sdk_client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        client_factory=_factory_for(captured, _FakeResult(content)),
    )
    sdk_out = await sdk_client.chat(messages)

    # OpenAI transport — fake the underlying ``chat.completions.create``.
    class _Msg:
        def __init__(self, c: str) -> None:
            self.content = c
            self.reasoning_content = ""

    class _Choice:
        def __init__(self, c: str) -> None:
            self.message = _Msg(c)
            self.finish_reason = "stop"

    class _Usage:
        prompt_tokens = 1
        completion_tokens = 1

    class _Completion:
        def __init__(self, c: str) -> None:
            self.choices = [_Choice(c)]
            self.usage = _Usage()

    openai_client = LMStudioClient(_settings())

    async def _fake_create(**kwargs: Any) -> _Completion:
        return _Completion(content)

    monkeypatch.setattr(openai_client._client.chat.completions, "create", _fake_create)
    openai_out = await openai_client.chat(messages)

    assert sdk_out == openai_out == content


@pytest.mark.asyncio
async def test_chat_passes_schema_response_format() -> None:
    captured: dict[str, Any] = {}
    client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        client_factory=_factory_for(captured, _FakeResult('{"ok": true}')),
    )
    schema = {"name": "snap", "schema": {"type": "object"}}
    await client.chat([{"role": "user", "content": "go"}], schema=schema)
    rf = captured["model"].respond_calls[0]["response_format"]
    assert rf == {"type": "json", "jsonSchema": {"type": "object"}}


@pytest.mark.asyncio
async def test_chat_reasoning_rides_on_config_raw() -> None:
    captured: dict[str, Any] = {}
    client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        model="role-model",
        reasoning="medium",
        client_factory=_factory_for(captured, _FakeResult("x")),
    )
    await client.chat([{"role": "user", "content": "go"}])
    config = captured["model"].respond_calls[0]["config"]
    assert config["raw"] == {"fields": [{"key": "reasoning", "value": "medium"}]}
    assert config["maxTokens"] == 4096
    # The pinned model is the one resolved from the handle.
    assert captured["client"].llm.model_keys == ["role-model"]


@pytest.mark.asyncio
async def test_chat_no_reasoning_omits_raw() -> None:
    captured: dict[str, Any] = {}
    client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        client_factory=_factory_for(captured, _FakeResult("x")),
    )
    await client.chat([{"role": "user", "content": "go"}])
    config = captured["model"].respond_calls[0]["config"]
    assert "raw" not in config


@pytest.mark.asyncio
async def test_chat_empty_content_raises() -> None:
    captured: dict[str, Any] = {}
    client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        client_factory=_factory_for(captured, _FakeResult("   ")),
    )
    with pytest.raises(LLMClientError, match="empty content"):
        await client.chat([{"role": "user", "content": "go"}])


@pytest.mark.asyncio
async def test_chat_sdk_error_maps_to_llm_client_error() -> None:
    captured: dict[str, Any] = {}
    client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        client_factory=_factory_for(captured, error=lmstudio.LMStudioServerError("boom")),
    )
    with pytest.raises(LLMClientError, match="SDK call failed"):
        await client.chat([{"role": "user", "content": "go"}])


@pytest.mark.asyncio
async def test_chat_falls_back_to_parsed() -> None:
    """An empty ``.content`` with a structured ``.parsed`` falls back to parsed."""

    captured: dict[str, Any] = {}
    client = LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk"),
        client_factory=_factory_for(captured, _FakeResult("", parsed={"ok": True})),
    )
    out = await client.chat([{"role": "user", "content": "go"}])
    assert out == '{"ok": true}'


# --- POC seams (downstream issues replace these) -----------------------------


@pytest.mark.asyncio
async def test_complete_not_implemented() -> None:
    client = LMStudioSDKClient(_settings(LLM_LMSTUDIO_TRANSPORT="sdk"))
    with pytest.raises(LLMClientError, match="issue 0113"):
        await client.complete([{"role": "user", "content": "go"}])


def test_is_llm_client_subclass() -> None:
    assert issubclass(LMStudioSDKClient, LLMClient)
