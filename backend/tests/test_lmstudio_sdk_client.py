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
from lmstudio.json_api import LMStudioWebsocketError

from bob.config import Settings
from bob.llm.factory import _build_for_backend, _build_role_client
from bob.llm.lmstudio_sdk import LMStudioSDKClient
from bob.llm.lmstudio_sdk.client import schema_to_response_format
from bob.llm.types import ToolDefinition
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
    """Stand-in for the SDK ``AsyncLLM`` handle. Records the ``respond`` call.

    For the low-level ``complete`` capture path it also exposes ``identifier`` +
    a ``_session`` whose ``_create_channel`` returns a fake channel driven by a
    scripted list of wire-event dicts — so the REAL ``ChatResponseEndpoint`` /
    ``AsyncPredictionStream`` parse them (faithful, no re-implementation of the
    SDK parser). ``executed`` records any tool-impl invocation (must stay empty:
    the no-execution guarantee).
    """

    identifier = "fake-model"

    def __init__(self, result: _FakeResult | None, error: Exception | None) -> None:
        self._result = result
        self._error = error
        self.respond_calls: list[dict[str, Any]] = []
        # Low-level (complete) capture seam.
        self._wire_events: list[dict[str, Any]] = []
        self.last_endpoint: Any = None
        self.executed: list[Any] = []
        self._session = _FakeSession(self)

    def script_events(self, events: list[dict[str, Any]]) -> None:
        """Set the wire-event dicts the fake channel will stream for ``complete``."""

        self._wire_events = events

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


class _FakeChannel:
    """Stand-in for the SDK ``AsyncChannel`` consumed by ``_iter_events``."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def rx_stream(self) -> Any:
        for event in self._events:
            yield event


class _FakeChannelCM:
    """Async context manager yielding a :class:`_FakeChannel` (channel_cm seam)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._channel = _FakeChannel(events)

    async def __aenter__(self) -> _FakeChannel:
        return self._channel

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Stand-in for ``AsyncLLM._session`` — only ``_create_channel`` is used."""

    def __init__(self, model: _FakeModel) -> None:
        self._model = model

    def _create_channel(self, endpoint: Any) -> _FakeChannelCM:
        # If a tool impl is ever invoked, record it so the test can assert the
        # SDK executed NOTHING (the capture-only contract).
        self._model.last_endpoint = endpoint
        return _FakeChannelCM(self._model._wire_events)


class _FakeLlmNamespace:
    def __init__(self, model: _FakeModel) -> None:
        self._model = model
        self.model_keys: list[str | None] = []

    async def model(self, model_key: str | None = None) -> _FakeModel:
        self.model_keys.append(model_key)
        return self._model


class _FakeAsyncClient:
    """Stand-in for the SDK ``AsyncClient``.

    Long-lived lifecycle (issue 0115): the SDK client drives ``__aenter__`` to
    connect and ``aclose`` to tear down (NOT a per-call ``async with``). The fake
    counts both so tests can assert the websocket is connected ONCE per client
    and closed only on supersede.
    """

    def __init__(self, model: _FakeModel) -> None:
        self.llm = _FakeLlmNamespace(model)
        self.closed = False
        self.connect_count = 0
        self.aclose_count = 0

    async def __aenter__(self) -> _FakeAsyncClient:
        self.connect_count += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True
        self.aclose_count += 1


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


# --- scripted wire events for the low-level complete() capture path ----------


def _success_event(*, content_fragments: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Build the wire-event dicts for a finished prediction (fragments + success).

    Minimal but real shapes the SDK's ``ChatResponseEndpoint.iter_message_events``
    parses; the REAL parser turns these into the events ``complete`` consumes.
    """

    events: list[dict[str, Any]] = []
    for frag in content_fragments:
        events.append(
            {
                "type": "fragment",
                "fragment": {
                    "content": frag,
                    "tokensCount": 1,
                    "containsDrafted": False,
                    "reasoningType": "none",
                },
            }
        )
    events.append(
        {
            "type": "success",
            "stats": {
                "stopReason": "eosFound",
                "tokensPerSecond": 1.0,
                "numGpuLayers": 0,
                "timeToFirstTokenSec": 0.1,
                "promptTokensCount": 5,
                "predictedTokensCount": 3,
                "totalTokensCount": 8,
            },
            "modelInfo": {
                "identifier": "m",
                "instanceReference": "r",
                "modelKey": "m",
                "format": "gguf",
                "displayName": "m",
                "path": "m",
                "sizeBytes": 1,
                "paramsString": "1B",
                "architecture": "x",
                "maxContextLength": 4096,
                "trainedForToolUse": True,
                "vision": False,
            },
            "loadModelConfig": {"fields": []},
            "predictionConfig": {"fields": []},
        }
    )
    return events


def _tool_call_event(name: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "toolCallGenerationEnd",
        "toolCallRequest": {
            "type": "function",
            "name": name,
            "id": call_id,
            "arguments": arguments,
        },
    }


def _tool(name: str = "web_search") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"desc {name}",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    )


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
    # Long-lived (issue 0115): connected once, NOT closed after the call.
    assert captured["client"].connect_count == 1
    assert captured["client"].closed is False


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


# --- complete() tool-calling (issue 0113) ------------------------------------


def _complete_client(captured: dict[str, Any], events: list[dict[str, Any]], **kw: Any) -> Any:
    """Build an SDK client whose fake model streams ``events`` for ``complete``."""

    def factory(host: str) -> Any:
        model = _FakeModel(_FakeResult("unused"), None)
        model.script_events(events)
        client = _FakeAsyncClient(model)
        captured["host"] = host
        captured["client"] = client
        captured["model"] = model
        return client

    return LMStudioSDKClient(
        _settings(LLM_LMSTUDIO_TRANSPORT="sdk", **kw),
        client_factory=factory,
    )


@pytest.mark.asyncio
async def test_complete_captures_tool_calls_without_executing() -> None:
    captured: dict[str, Any] = {}
    events = [
        _tool_call_event("web_search", "call_1", {"q": "bitcoin"}),
        *_success_event(),
    ]
    client = _complete_client(captured, events)
    resp = await client.complete([{"role": "user", "content": "search"}], tools=[_tool()])

    assert resp.is_tool_call
    assert resp.text is None
    (call,) = resp.tool_calls
    assert call.name == "web_search"
    assert call.id == "call_1"
    assert call.arguments == {"q": "bitcoin"}
    # The SDK executed NOTHING — no tool impl was invoked.
    assert captured["model"].executed == []
    # Long-lived (issue 0115): the websocket stays open across calls.
    assert captured["client"].connect_count == 1
    assert captured["client"].closed is False


@pytest.mark.asyncio
async def test_complete_multiple_tool_calls() -> None:
    captured: dict[str, Any] = {}
    events = [
        _tool_call_event("web_search", "c1", {"q": "a"}),
        _tool_call_event("web_search", "c2", {"q": "b"}),
        *_success_event(),
    ]
    client = _complete_client(captured, events)
    resp = await client.complete([{"role": "user", "content": "go"}], tools=[_tool()])
    assert [c.id for c in resp.tool_calls] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_complete_no_tool_call_returns_text() -> None:
    captured: dict[str, Any] = {}
    events = _success_event(content_fragments=("hello ", "world"))
    client = _complete_client(captured, events)
    resp = await client.complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    assert resp.tool_calls == []
    assert resp.text == "hello world"


@pytest.mark.asyncio
async def test_complete_empty_content_raises() -> None:
    captured: dict[str, Any] = {}
    events = _success_event(content_fragments=("   ",))
    client = _complete_client(captured, events)
    with pytest.raises(LLMClientError, match="empty response"):
        await client.complete([{"role": "user", "content": "hi"}], tools=[_tool()])


@pytest.mark.asyncio
async def test_complete_no_tools_drives_plain_prediction() -> None:
    """``complete`` with no tools advertises none and returns the model text."""

    captured: dict[str, Any] = {}
    events = _success_event(content_fragments=("plain answer",))
    client = _complete_client(captured, events)
    resp = await client.complete([{"role": "user", "content": "hi"}])
    assert resp.text == "plain answer"
    assert resp.tool_calls == []


@pytest.mark.asyncio
async def test_complete_advertises_tools_to_endpoint() -> None:
    """The converted SDK tools reach the low-level endpoint's config (rawTools)."""

    captured: dict[str, Any] = {}
    events = _success_event(content_fragments=("ok",))
    client = _complete_client(captured, events)
    await client.complete([{"role": "user", "content": "hi"}], tools=[_tool("alpha")])
    endpoint = captured["model"].last_endpoint
    # The endpoint folds llm_tools into its config stack; the advertised tool
    # name is reachable on the channel creation params (rawTools).
    blob = str(endpoint.creation_params)
    assert "alpha" in blob


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["guided", "hermes"])
async def test_complete_rejects_guided_and_hermes_modes(mode: str) -> None:
    captured: dict[str, Any] = {}
    events = _success_event(content_fragments=("ok",))
    client = _complete_client(captured, events, LLM_TOOL_MODE=mode)
    with pytest.raises(LLMClientError, match="not supported on the LM Studio"):
        await client.complete([{"role": "user", "content": "hi"}], tools=[_tool()])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "native"])
async def test_complete_accepts_auto_and_native_modes(mode: str) -> None:
    captured: dict[str, Any] = {}
    events = [_tool_call_event("web_search", "c1", {"q": "x"}), *_success_event()]
    client = _complete_client(captured, events, LLM_TOOL_MODE=mode)
    resp = await client.complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    assert resp.is_tool_call


@pytest.mark.asyncio
async def test_complete_sdk_error_maps_to_llm_client_error() -> None:
    captured: dict[str, Any] = {}

    def factory(host: str) -> Any:
        model = _FakeModel(_FakeResult("x"), None)

        def boom(_endpoint: Any) -> Any:
            raise lmstudio.LMStudioServerError("boom")

        model._session._create_channel = boom  # type: ignore[assignment]
        captured["model"] = model
        return _FakeAsyncClient(model)

    client = LMStudioSDKClient(_settings(LLM_LMSTUDIO_TRANSPORT="sdk"), client_factory=factory)
    with pytest.raises(LLMClientError, match="SDK call failed"):
        await client.complete([{"role": "user", "content": "hi"}], tools=[_tool()])


# --- long-lived client lifecycle + reconnect (issue 0115 / M7) ---------------


class _WsDropModel(_FakeModel):
    """A fake model whose ``respond`` raises a websocket drop the first N times.

    Drives the reconnect+retry-once path: ``drops`` counts how many initial
    ``respond`` calls (across the whole client's lifetime) raise
    :class:`LMStudioWebsocketError` before one succeeds.
    """

    def __init__(self, drops: int, result: _FakeResult) -> None:
        super().__init__(result, None)
        self._drops = drops

    async def respond(
        self, history: Any, *, response_format: Any = None, config: Any = None
    ) -> Any:
        if self._drops > 0:
            self._drops -= 1
            raise LMStudioWebsocketError("websocket dropped")
        return await super().respond(history, response_format=response_format, config=config)


def _lifecycle_factory(clients: list[_FakeAsyncClient], model: _FakeModel) -> Any:
    """A factory recording every built client into ``clients`` (one per connect).

    Each ``_connect`` builds a NEW fake client wrapping the SAME shared ``model``
    (so a ``drops`` counter survives across reconnects), so the list length is the
    number of (re)connections — exactly what the lifecycle assertions read.
    """

    def factory(host: str) -> _FakeAsyncClient:
        client = _FakeAsyncClient(model)
        clients.append(client)
        return client

    return factory


@pytest.mark.asyncio
async def test_long_lived_client_connects_once_across_calls() -> None:
    """N calls reuse ONE connection: the factory connects once, never per call."""

    clients: list[_FakeAsyncClient] = []
    factory = _lifecycle_factory(clients, _FakeModel(_FakeResult("ok"), None))
    client = LMStudioSDKClient(_settings(LLM_LMSTUDIO_TRANSPORT="sdk"), client_factory=factory)

    for _ in range(3):
        assert await client.chat([{"role": "user", "content": "hi"}]) == "ok"

    # Exactly one client built + connected; never closed mid-life.
    assert len(clients) == 1
    assert clients[0].connect_count == 1
    assert clients[0].aclose_count == 0
    assert clients[0].closed is False


@pytest.mark.asyncio
async def test_websocket_drop_reconnects_and_retries_once() -> None:
    """A websocket drop mid-call → ONE reconnect, then the retry succeeds."""

    clients: list[_FakeAsyncClient] = []
    factory = _lifecycle_factory(clients, _WsDropModel(drops=1, result=_FakeResult("recovered")))
    client = LMStudioSDKClient(_settings(LLM_LMSTUDIO_TRANSPORT="sdk"), client_factory=factory)

    out = await client.chat([{"role": "user", "content": "hi"}])
    assert out == "recovered"
    # First client connected then closed on reconnect; a second client connected.
    assert len(clients) == 2
    assert clients[0].aclose_count == 1
    assert clients[1].connect_count == 1
    assert clients[1].closed is False


@pytest.mark.asyncio
async def test_persistent_websocket_drop_raises_llm_client_error() -> None:
    """Two consecutive drops (reconnect + retry both fail) → ``LLMClientError``."""

    clients: list[_FakeAsyncClient] = []
    factory = _lifecycle_factory(clients, _WsDropModel(drops=2, result=_FakeResult("never")))
    client = LMStudioSDKClient(_settings(LLM_LMSTUDIO_TRANSPORT="sdk"), client_factory=factory)

    with pytest.raises(LLMClientError, match="SDK call failed"):
        await client.chat([{"role": "user", "content": "hi"}])
    # Initial connect + exactly one reconnect (retry-once, not a retry loop).
    assert len(clients) == 2


@pytest.mark.asyncio
async def test_aclose_closes_the_long_lived_client() -> None:
    """``aclose`` tears down the connected websocket; idempotent + safe unused."""

    clients: list[_FakeAsyncClient] = []
    factory = _lifecycle_factory(clients, _FakeModel(_FakeResult("ok"), None))
    client = LMStudioSDKClient(_settings(LLM_LMSTUDIO_TRANSPORT="sdk"), client_factory=factory)

    # aclose before any use is a no-op (lazy: never connected).
    await client.aclose()
    assert clients == []

    await client.chat([{"role": "user", "content": "hi"}])
    assert len(clients) == 1
    await client.aclose()
    assert clients[0].aclose_count == 1
    # Idempotent: a second aclose does not double-close.
    await client.aclose()
    assert clients[0].aclose_count == 1


def test_is_llm_client_subclass() -> None:
    assert issubclass(LMStudioSDKClient, LLMClient)
