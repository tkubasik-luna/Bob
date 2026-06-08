"""Tests for SDK streaming tool-calls + the M6 arg-fragment override (issue 0114).

Two layers, both fully offline + deterministic (no running LM Studio server):

1. ``stream_complete`` end-to-end: a fake channel streams REAL LM Studio wire
   message dicts (including the incremental
   ``toolCallGenerationArgumentFragmentGenerated`` fragments) which the REAL
   ``BobChatResponseEndpoint`` (M6) parses — so the override + the StreamChunk
   lifecycle are exercised faithfully, no re-implementation of the SDK parser.

2. The M6 CONTRACT GUARD: feed ``BobChatResponseEndpoint.iter_message_events``
   faked channel-message dicts directly and assert the override resurfaces the
   dropped arg fragment as a ``PredictionToolCallArgFragmentEvent``. This fails
   loudly if a future SDK upgrade renames the wire message / its ``content`` key
   / reintegrates the fragment into the base parser.

The HARD acceptance criterion (PRD 0017 / issue 0114) is asserted explicitly:
at least one ``tool_call_args_delta`` is emitted BEFORE ``tool_call_end`` so the
``say`` tool starts TTS early (latency parity with the OpenAI transport).
"""

from __future__ import annotations

from typing import Any

import lmstudio
import pytest

from bob.config import Settings
from bob.llm.lmstudio_sdk import LMStudioSDKClient
from bob.llm.lmstudio_sdk.endpoint import (
    BobChatResponseEndpoint,
    PredictionToolCallArgFragmentEvent,
)
from bob.llm.types import StreamChunk, ToolDefinition
from bob.llm_client import LLMClientError


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "LLM_PROVIDER": "lm_studio",
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_MODEL": "qwen2.5-7b-instruct",
        "LLM_API_KEY": "lm-studio",
        "LLM_LMSTUDIO_TRANSPORT": "sdk",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- SDK fakes (channel streams REAL wire dicts; REAL endpoint parses them) ----


class _FakeChannel:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def rx_stream(self) -> Any:
        for event in self._events:
            yield event


class _FakeChannelCM:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._channel = _FakeChannel(events)

    async def __aenter__(self) -> _FakeChannel:
        return self._channel

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self, model: _FakeModel) -> None:
        self._model = model

    def _create_channel(self, endpoint: Any) -> _FakeChannelCM:
        self._model.last_endpoint = endpoint
        return _FakeChannelCM(self._model._wire_events)


class _FakeModel:
    identifier = "fake-model"

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._wire_events = events
        self.last_endpoint: Any = None
        self.executed: list[Any] = []
        self._session = _FakeSession(self)

    async def model(self, *_a: Any, **_k: Any) -> _FakeModel:  # pragma: no cover
        return self


class _FakeLlmNamespace:
    def __init__(self, model: _FakeModel) -> None:
        self._model = model
        self.model_keys: list[str | None] = []

    async def model(self, model_key: str | None = None) -> _FakeModel:
        self.model_keys.append(model_key)
        return self._model


class _FakeAsyncClient:
    def __init__(self, model: _FakeModel) -> None:
        self.llm = _FakeLlmNamespace(model)
        self.closed = False

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


def _stream_client(captured: dict[str, Any], events: list[dict[str, Any]], **kw: Any) -> Any:
    def factory(host: str) -> Any:
        model = _FakeModel(events)
        client = _FakeAsyncClient(model)
        captured["host"] = host
        captured["client"] = client
        captured["model"] = model
        return client

    return LMStudioSDKClient(
        _settings(**kw),
        client_factory=factory,
    )


def _tool(name: str = "say") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"desc {name}",
        parameters={
            "type": "object",
            "properties": {"speech": {"type": "string"}},
            "required": ["speech"],
        },
    )


# --- real LM Studio wire-message dict builders -------------------------------


def _start(tool_call_id: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": "toolCallGenerationStart"}
    if tool_call_id is not None:
        msg["toolCallId"] = tool_call_id
    return msg


def _name(name: str) -> dict[str, Any]:
    return {"type": "toolCallGenerationNameReceived", "name": name}


def _arg_fragment(content: str) -> dict[str, Any]:
    return {"type": "toolCallGenerationArgumentFragmentGenerated", "content": content}


def _tool_end(name: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "toolCallGenerationEnd",
        "toolCallRequest": {
            "type": "function",
            "name": name,
            "id": call_id,
            "arguments": arguments,
        },
    }


def _content_fragment(content: str, reasoning_type: str = "none") -> dict[str, Any]:
    return {
        "type": "fragment",
        "fragment": {
            "content": content,
            "tokensCount": 1,
            "containsDrafted": False,
            "reasoningType": reasoning_type,
        },
    }


def _success() -> dict[str, Any]:
    return {
        "type": "success",
        "stats": {
            "stopReason": "eosFound",
            "tokensPerSecond": 42.0,
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


async def _collect(agen: Any) -> list[StreamChunk]:
    return [chunk async for chunk in agen]


# --- M6 contract guard (the anti-regression sentinel) ------------------------


def _bob_endpoint(tools: list[ToolDefinition] | None = None) -> BobChatResponseEndpoint:
    """Build a real ``BobChatResponseEndpoint`` (no channel) for direct feeding."""

    from bob.llm.lmstudio_sdk.history import messages_to_chat
    from bob.llm.lmstudio_sdk.tools import tool_definitions_to_sdk

    chat = messages_to_chat([{"role": "user", "content": "go"}])
    llm_tools, client_tool_map = (None, None)
    if tools:
        llm_tools, client_tool_map = tool_definitions_to_sdk(tools)
    return BobChatResponseEndpoint(
        "fake-model",
        chat,
        None,
        {"maxTokens": 4096},
        None,
        None,
        None,
        None,
        None,
        None,
        llm_tools,
        client_tool_map,
    )


def test_guard_arg_fragment_resurfaced() -> None:
    """The override resurfaces the dropped arg fragment as a custom event.

    SENTINEL: if a future SDK upgrade renames
    ``toolCallGenerationArgumentFragmentGenerated`` / moves the fragment off the
    ``content`` key / reintegrates it into the base parser, this assertion breaks
    loudly — exactly the anti-regression guard the PRD demands.
    """

    endpoint = _bob_endpoint([_tool()])
    # Feed the dropped messages directly, in wire order.
    list(endpoint.iter_message_events(_start("call_abc")))
    list(endpoint.iter_message_events(_name("say")))
    events = list(endpoint.iter_message_events(_arg_fragment('{"speech": "Hel')))

    assert len(events) == 1
    (event,) = events
    assert isinstance(event, PredictionToolCallArgFragmentEvent)
    assert event.fragment == '{"speech": "Hel'
    assert event.id == "call_abc"
    assert event.name == "say"
    assert event.index == 0


def test_guard_arg_fragment_index_increments_per_call() -> None:
    """A second ``toolCallGenerationStart`` advances the tracked call index."""

    endpoint = _bob_endpoint([_tool()])
    list(endpoint.iter_message_events(_start("c1")))
    (e0,) = list(endpoint.iter_message_events(_arg_fragment("a")))
    list(endpoint.iter_message_events(_start("c2")))
    (e1,) = list(endpoint.iter_message_events(_arg_fragment("b")))
    assert (e0.index, e0.id) == (0, "c1")  # type: ignore[union-attr]
    assert (e1.index, e1.id) == (1, "c2")  # type: ignore[union-attr]


def test_guard_non_fragment_messages_delegate_to_base() -> None:
    """A normal content fragment falls through to the base SDK parser unchanged."""

    from lmstudio.json_api import PredictionFragmentEvent

    endpoint = _bob_endpoint([_tool()])
    events = list(endpoint.iter_message_events(_content_fragment("hello")))
    assert len(events) == 1
    (event,) = events
    assert isinstance(event, PredictionFragmentEvent)
    assert event.arg.content == "hello"


def test_guard_empty_fragment_emits_nothing() -> None:
    """An empty-``content`` fragment yields no event (no no-op delta)."""

    endpoint = _bob_endpoint([_tool()])
    list(endpoint.iter_message_events(_start("c1")))
    assert list(endpoint.iter_message_events(_arg_fragment(""))) == []


def test_guard_custom_event_survives_handle_rx_event() -> None:
    """``handle_rx_event`` no-ops our custom event (base would ``assert_never``)."""

    endpoint = _bob_endpoint([_tool()])
    # Must not raise.
    endpoint.handle_rx_event(PredictionToolCallArgFragmentEvent(fragment="x", index=0))


# --- stream_complete end-to-end ----------------------------------------------


@pytest.mark.asyncio
async def test_stream_complete_tool_call_lifecycle() -> None:
    """start → ≥1 incremental args_delta → end, no execution; args reconstructed."""

    captured: dict[str, Any] = {}
    events = [
        _start("call_1"),
        _name("say"),
        _arg_fragment('{"speech": "Hel'),
        _arg_fragment("lo wor"),
        _arg_fragment('ld"}'),
        _tool_end("say", "call_1", {"speech": "Hello world"}),
        _success(),
    ]
    client = _stream_client(captured, events)
    chunks = await _collect(
        await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    )

    kinds = [c.kind for c in chunks]
    assert kinds[0] == "tool_call_start"
    assert kinds.count("tool_call_args_delta") == 3
    assert "tool_call_end" in kinds
    assert kinds[-1] == "perf"

    # The incremental deltas reconstruct the streamed argument string.
    deltas = [c.args_delta for c in chunks if c.kind == "tool_call_args_delta"]
    assert "".join(deltas) == '{"speech": "Hello world"}'

    start = next(c for c in chunks if c.kind == "tool_call_start")
    assert start.tool_call_id == "call_1"
    assert start.name == "say"

    end = next(c for c in chunks if c.kind == "tool_call_end")
    assert end.final_arguments == {"speech": "Hello world"}

    # No tool was executed (capture-only contract); websocket closed.
    assert captured["model"].executed == []
    assert captured["client"].closed is True


@pytest.mark.asyncio
async def test_stream_complete_args_delta_before_end_HARD() -> None:
    """HARD criterion: a ``tool_call_args_delta`` is emitted BEFORE ``tool_call_end``.

    This is what preserves early-TTS start for the ``say`` tool — the whole point
    of the M6 override.
    """

    captured: dict[str, Any] = {}
    events = [
        _start("call_1"),
        _name("say"),
        _arg_fragment('{"speech": "Hi"}'),
        _tool_end("say", "call_1", {"speech": "Hi"}),
        _success(),
    ]
    client = _stream_client(captured, events)
    chunks = await _collect(
        await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    )
    kinds = [c.kind for c in chunks]
    first_delta = kinds.index("tool_call_args_delta")
    end = kinds.index("tool_call_end")
    assert first_delta < end


@pytest.mark.asyncio
async def test_stream_complete_malformed_final_args_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed final arguments → ``LLMClientError`` (parity with ``complete``).

    The SDK's ``ToolCallRequest`` schema enforces ``arguments`` is a Mapping (a
    non-object can never reach us off the wire — the SDK rejects it at parse).
    The parity surface ``tool_call_request_to_tool_call`` rejects a non-``dict``
    Mapping, so we drive a terminal ``PredictionToolCallEvent`` whose request
    carries a ``MappingProxyType`` (a real, non-``dict`` Mapping) — exactly the
    malformed shape ``complete`` raises on too.
    """

    from types import MappingProxyType

    from lmstudio._sdk_models import ToolCallRequest
    from lmstudio.json_api import PredictionToolCallEvent

    from bob.llm.lmstudio_sdk import endpoint as endpoint_mod

    bad_request = ToolCallRequest(
        type="function",
        name="say",
        id="call_1",
        arguments=MappingProxyType({"speech": "hi"}),
    )

    captured: dict[str, Any] = {}
    events = [_start("call_1"), _name("say"), _arg_fragment('{"speech": "hi"}'), _success()]
    client = _stream_client(captured, events)

    # Make the real endpoint yield the bad terminal event before ``success``.
    base_iter = endpoint_mod.BobChatResponseEndpoint.iter_message_events

    def _patched(self: Any, contents: Any) -> Any:
        if isinstance(contents, dict) and contents.get("type") == "success":
            yield PredictionToolCallEvent(bad_request)
        yield from base_iter(self, contents)

    monkeypatch.setattr(endpoint_mod.BobChatResponseEndpoint, "iter_message_events", _patched)

    with pytest.raises(LLMClientError, match="not a"):
        await _collect(
            await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
        )


@pytest.mark.asyncio
async def test_stream_complete_text_mode_no_tool_call() -> None:
    """No tool-call → ``text`` chunks + a terminal ``perf`` chunk."""

    captured: dict[str, Any] = {}
    events = [
        _content_fragment("hello "),
        _content_fragment("world"),
        _success(),
    ]
    client = _stream_client(captured, events)
    chunks = await _collect(
        await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    )
    text = "".join(c.text_delta for c in chunks if c.kind == "text")
    assert text == "hello world"
    assert [c.kind for c in chunks if c.kind != "text"] == ["perf"]
    assert not any(c.kind.startswith("tool_call") for c in chunks)


@pytest.mark.asyncio
async def test_stream_complete_reasoning_passthrough() -> None:
    """Reasoning fragments → ``reasoning`` chunks (cosmetic, not in text)."""

    captured: dict[str, Any] = {}
    events = [
        _content_fragment("thinking...", reasoning_type="reasoning"),
        _content_fragment("answer"),
        _success(),
    ]
    client = _stream_client(captured, events)
    chunks = await _collect(
        await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    )
    reasoning = "".join(c.reasoning_delta for c in chunks if c.kind == "reasoning")
    text = "".join(c.text_delta for c in chunks if c.kind == "text")
    assert reasoning == "thinking..."
    assert text == "answer"


@pytest.mark.asyncio
async def test_stream_complete_perf_chunk_carries_stats() -> None:
    """The terminal ``perf`` chunk carries tokens / ttft / tok_s from SDK stats."""

    captured: dict[str, Any] = {}
    events = [_content_fragment("ok"), _success()]
    client = _stream_client(captured, events)
    chunks = await _collect(
        await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    )
    perf = chunks[-1]
    assert perf.kind == "perf"
    assert perf.tokens_in == 5
    assert perf.tokens_out == 3
    assert perf.ttft_s == 0.1
    assert perf.tok_s == 42.0


@pytest.mark.asyncio
async def test_stream_complete_whole_call_only_still_well_formed() -> None:
    """A model that emits ONLY ``toolCallGenerationEnd`` still yields start→end.

    Uncommon (no streamed fragments), but the lifecycle must stay well-formed.
    """

    captured: dict[str, Any] = {}
    events = [
        _tool_end("say", "call_1", {"speech": "Hi"}),
        _success(),
    ]
    client = _stream_client(captured, events)
    chunks = await _collect(
        await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
    )
    kinds = [c.kind for c in chunks]
    assert kinds == ["tool_call_start", "tool_call_end", "perf"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["guided", "hermes"])
async def test_stream_complete_rejects_guided_and_hermes(mode: str) -> None:
    captured: dict[str, Any] = {}
    events = [_content_fragment("ok"), _success()]
    client = _stream_client(captured, events, LLM_TOOL_MODE=mode)
    with pytest.raises(LLMClientError, match="not supported on the LM Studio"):
        await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])


@pytest.mark.asyncio
async def test_stream_complete_sdk_error_maps_to_llm_client_error() -> None:
    captured: dict[str, Any] = {}

    def factory(host: str) -> Any:
        model = _FakeModel([])

        def boom(_endpoint: Any) -> Any:
            raise lmstudio.LMStudioServerError("boom")

        model._session._create_channel = boom  # type: ignore[assignment]
        captured["model"] = model
        return _FakeAsyncClient(model)

    client = LMStudioSDKClient(_settings(), client_factory=factory)
    with pytest.raises(LLMClientError, match="SDK stream failed"):
        await _collect(
            await client.stream_complete([{"role": "user", "content": "hi"}], tools=[_tool()])
        )
