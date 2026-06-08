"""Tests for the LM Studio SDK streaming adapter (module M5) + ``stream_chat``.

The ``lmstudio`` SDK is faked at its boundary exactly like
``test_lmstudio_sdk_client``: a scripted async fragment iterator drives the M5
adapter in isolation, and a fake ``AsyncClient`` whose ``respond_stream`` yields
that scripted iterator drives ``LMStudioSDKClient.stream_chat`` end-to-end. Fully
offline + deterministic — no running LM Studio server.

Coverage:
- M5 adapter, scripted: text-only; reasoning+text interleaved; final stats → perf.
- ``stream_chat`` end-to-end: text + reasoning ticks then a terminal perf chunk.
- byte-identity: concatenated ``text`` deltas == what ``chat()`` returns for the
  same scripted content.
- guided-JSON (``schema``) streaming path passes ``response_format`` through.
- ``log_llm_call`` + the end debug event emitted with aggregated text + tokens.
- an SDK ``LMStudioError`` from ``respond_stream`` → :class:`LLMClientError`.
"""

from __future__ import annotations

from typing import Any

import lmstudio
import pytest

from bob.config import Settings
from bob.llm.lmstudio_sdk import LMStudioSDKClient
from bob.llm.lmstudio_sdk.streaming import (
    adapt_prediction_stream,
    build_perf_chunk,
    fragment_to_chunk,
    is_reasoning_fragment,
)
from bob.llm.types import StreamChunk
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


# --- SDK streaming fakes -----------------------------------------------------


class _FakeFragment:
    """Stand-in for the SDK ``LlmPredictionFragment``."""

    def __init__(self, content: str, reasoning_type: str = "none") -> None:
        self.content = content
        self.reasoning_type = reasoning_type
        self.tokens_count = 1
        self.contains_drafted = False


class _FakeStreamStats:
    """Stand-in for the SDK ``LlmPredictionStats``."""

    def __init__(
        self,
        *,
        prompt: int | None = 11,
        predicted: int | None = 7,
        ttft: float | None = 0.25,
        tok_s: float | None = 42.0,
    ) -> None:
        self.prompt_tokens_count = prompt
        self.predicted_tokens_count = predicted
        self.time_to_first_token_sec = ttft
        self.tokens_per_second = tok_s


class _FakeStream:
    """Stand-in for the SDK ``AsyncPredictionStream``.

    Iterable (yields fragments), exposes ``.stats`` only AFTER iteration drains
    (mirrors the real stream, where the final result lands at stream end).
    """

    def __init__(self, fragments: list[_FakeFragment], stats: _FakeStreamStats | None) -> None:
        self._fragments = fragments
        self._stats = stats
        self.stats: _FakeStreamStats | None = None

    async def __aiter__(self) -> Any:
        for frag in self._fragments:
            yield frag
        self.stats = self._stats


class _FakeStreamModel:
    """Fake ``AsyncLLM`` whose ``respond_stream`` returns a scripted stream."""

    def __init__(
        self,
        stream: _FakeStream | None,
        error: Exception | None = None,
    ) -> None:
        self._stream = stream
        self._error = error
        self.respond_stream_calls: list[dict[str, Any]] = []

    async def respond_stream(
        self,
        history: Any,
        *,
        response_format: Any = None,
        config: Any = None,
    ) -> _FakeStream:
        self.respond_stream_calls.append(
            {"history": history, "response_format": response_format, "config": config}
        )
        if self._error is not None:
            raise self._error
        assert self._stream is not None
        return self._stream

    async def respond(
        self,
        history: Any,
        *,
        response_format: Any = None,
        config: Any = None,
    ) -> Any:
        # Used by the byte-identity test: non-streaming counterpart returns the
        # concatenation of the scripted CONTENT (non-reasoning) fragments.
        assert self._stream is not None
        content = "".join(f.content for f in self._stream._fragments if f.reasoning_type == "none")

        class _Result:
            def __init__(self, c: str) -> None:
                self.content = c
                self.parsed = None
                self.stats = _FakeStreamStats()

        return _Result(content)


class _FakeLlmNamespace:
    def __init__(self, model: _FakeStreamModel) -> None:
        self._model = model
        self.model_keys: list[str | None] = []

    async def model(self, model_key: str | None = None) -> _FakeStreamModel:
        self.model_keys.append(model_key)
        return self._model


class _FakeAsyncClient:
    def __init__(self, model: _FakeStreamModel) -> None:
        self.llm = _FakeLlmNamespace(model)
        self.closed = False

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


def _factory_for(
    captured: dict[str, Any],
    stream: _FakeStream | None,
    error: Exception | None = None,
) -> Any:
    def factory(host: str) -> Any:
        model = _FakeStreamModel(stream, error)
        client = _FakeAsyncClient(model)
        captured["host"] = host
        captured["client"] = client
        captured["model"] = model
        return client

    return factory


async def _scripted_iter(fragments: list[_FakeFragment]) -> Any:
    for frag in fragments:
        yield frag


# --- M5 adapter (isolated, scripted) -----------------------------------------


def test_is_reasoning_fragment() -> None:
    assert is_reasoning_fragment(_FakeFragment("x", "reasoning")) is True
    assert is_reasoning_fragment(_FakeFragment("x", "reasoningStartTag")) is True
    assert is_reasoning_fragment(_FakeFragment("x", "reasoningEndTag")) is True
    assert is_reasoning_fragment(_FakeFragment("x", "none")) is False


def test_fragment_to_chunk_maps_kinds() -> None:
    text = fragment_to_chunk(_FakeFragment("hi", "none"))
    assert text == StreamChunk(kind="text", text_delta="hi")
    reasoning = fragment_to_chunk(_FakeFragment("ponder", "reasoning"))
    assert reasoning == StreamChunk(kind="reasoning", reasoning_delta="ponder")
    assert fragment_to_chunk(_FakeFragment("", "none")) is None


def test_build_perf_chunk_prefers_sdk_stats() -> None:
    stats = _FakeStreamStats(prompt=11, predicted=7, ttft=0.25, tok_s=42.0)
    chunk = build_perf_chunk(stats, started=0.0, first_token_at=1.0)
    assert chunk.kind == "perf"
    assert chunk.tokens_in == 11
    assert chunk.tokens_out == 7
    assert chunk.ttft_s == 0.25
    assert chunk.tok_s == 42.0
    assert chunk.reasoning_tokens is None


def test_build_perf_chunk_none_stats_uses_measured_ttft() -> None:
    chunk = build_perf_chunk(None, started=1.0, first_token_at=1.5)
    assert chunk.tokens_in is None
    assert chunk.tokens_out is None
    assert chunk.ttft_s == 0.5
    assert chunk.tok_s is None


@pytest.mark.asyncio
async def test_adapter_text_only() -> None:
    frags = [_FakeFragment("hel"), _FakeFragment("lo")]
    chunks = [
        c
        async for c in adapt_prediction_stream(
            _scripted_iter(frags),
            stats_getter=lambda: _FakeStreamStats(),
            started=0.0,
        )
    ]
    assert [c.kind for c in chunks] == ["text", "text", "perf"]
    assert "".join(c.text_delta for c in chunks if c.kind == "text") == "hello"


@pytest.mark.asyncio
async def test_adapter_reasoning_then_text() -> None:
    frags = [
        _FakeFragment("think", "reasoning"),
        _FakeFragment("ing", "reasoning"),
        _FakeFragment("answer", "none"),
    ]
    chunks = [
        c
        async for c in adapt_prediction_stream(
            _scripted_iter(frags),
            stats_getter=lambda: _FakeStreamStats(),
            started=0.0,
        )
    ]
    assert [c.kind for c in chunks] == ["reasoning", "reasoning", "text", "perf"]
    assert "".join(c.reasoning_delta for c in chunks if c.kind == "reasoning") == "thinking"
    assert "".join(c.text_delta for c in chunks if c.kind == "text") == "answer"


@pytest.mark.asyncio
async def test_adapter_on_text_accumulates_only_text() -> None:
    frags = [
        _FakeFragment("r", "reasoning"),
        _FakeFragment("a", "none"),
        _FakeFragment("b", "none"),
    ]
    acc: list[str] = []
    async for _ in adapt_prediction_stream(
        _scripted_iter(frags),
        stats_getter=lambda: None,
        started=0.0,
        on_text=acc.append,
    ):
        pass
    assert "".join(acc) == "ab"


# --- stream_chat end-to-end --------------------------------------------------


@pytest.mark.asyncio
async def test_stream_chat_yields_text_reasoning_then_perf() -> None:
    captured: dict[str, Any] = {}
    frags = [
        _FakeFragment("mull", "reasoning"),
        _FakeFragment("Hello", "none"),
        _FakeFragment(" world", "none"),
    ]
    stream = _FakeStream(frags, _FakeStreamStats(prompt=11, predicted=7))
    client = LMStudioSDKClient(_settings(), client_factory=_factory_for(captured, stream))

    chunks = [c async for c in await client.stream_chat([{"role": "user", "content": "hi"}])]

    assert [c.kind for c in chunks] == ["reasoning", "text", "text", "perf"]
    assert "".join(c.text_delta for c in chunks if c.kind == "text") == "Hello world"
    perf = chunks[-1]
    assert perf.tokens_in == 11
    assert perf.tokens_out == 7
    # Client opened + closed, model resolved from the pinned model.
    assert captured["host"] == "localhost:1234"
    assert captured["client"].closed is True


@pytest.mark.asyncio
async def test_stream_chat_byte_identity_with_chat() -> None:
    """Concatenated ``text`` deltas == what ``chat()`` returns for same content."""

    frags = [
        _FakeFragment("noise", "reasoning"),
        _FakeFragment('{"act', "none"),
        _FakeFragment('ion":"x"}', "none"),
    ]

    cap_stream: dict[str, Any] = {}
    stream = _FakeStream(frags, _FakeStreamStats())
    stream_client = LMStudioSDKClient(_settings(), client_factory=_factory_for(cap_stream, stream))
    streamed = [
        c async for c in await stream_client.stream_chat([{"role": "user", "content": "go"}])
    ]
    aggregated = "".join(c.text_delta for c in streamed if c.kind == "text")

    cap_chat: dict[str, Any] = {}
    # Reuse the SAME scripted fragments so ``chat()``'s fake ``respond`` returns
    # the concatenation of the content (non-reasoning) fragments.
    chat_stream = _FakeStream(frags, _FakeStreamStats())
    chat_client = LMStudioSDKClient(_settings(), client_factory=_factory_for(cap_chat, chat_stream))
    chat_out = await chat_client.chat([{"role": "user", "content": "go"}])

    assert aggregated == chat_out == '{"action":"x"}'


@pytest.mark.asyncio
async def test_stream_chat_passes_schema_response_format() -> None:
    captured: dict[str, Any] = {}
    stream = _FakeStream([_FakeFragment('{"ok": true}', "none")], _FakeStreamStats())
    client = LMStudioSDKClient(_settings(), client_factory=_factory_for(captured, stream))
    schema = {"name": "snap", "schema": {"type": "object"}}
    async for _ in await client.stream_chat([{"role": "user", "content": "go"}], schema=schema):
        pass
    rf = captured["model"].respond_stream_calls[0]["response_format"]
    assert rf == {"type": "json", "jsonSchema": {"type": "object"}}


@pytest.mark.asyncio
async def test_stream_chat_reasoning_rides_on_config_raw() -> None:
    captured: dict[str, Any] = {}
    stream = _FakeStream([_FakeFragment("x", "none")], _FakeStreamStats())
    client = LMStudioSDKClient(
        _settings(),
        model="role-model",
        reasoning="high",
        client_factory=_factory_for(captured, stream),
    )
    async for _ in await client.stream_chat([{"role": "user", "content": "go"}]):
        pass
    config = captured["model"].respond_stream_calls[0]["config"]
    assert config["raw"] == {"fields": [{"key": "reasoning", "value": "high"}]}
    assert config["maxTokens"] == 4096


@pytest.mark.asyncio
async def test_stream_chat_logs_and_emits_end_event(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    import bob.llm.lmstudio_sdk.client as client_mod

    def _fake_log(**kwargs: Any) -> None:
        logged.append(kwargs)

    def _fake_emit(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(client_mod, "log_llm_call", _fake_log)
    monkeypatch.setattr(client_mod, "emit_debug", _fake_emit)

    captured: dict[str, Any] = {}
    frags = [_FakeFragment("Hello", "none"), _FakeFragment(" there", "none")]
    stream = _FakeStream(frags, _FakeStreamStats(prompt=11, predicted=7))
    client = LMStudioSDKClient(_settings(), client_factory=_factory_for(captured, stream))

    async for _ in await client.stream_chat([{"role": "user", "content": "hi"}]):
        pass

    assert len(logged) == 1
    assert logged[0]["raw_response"] == "Hello there"
    assert logged[0]["tokens_in"] == 11
    assert logged[0]["tokens_out"] == 7

    end_events = [e for e in events if e["summary"].startswith("LLM chat stream terminé")]
    assert len(end_events) == 1
    assert end_events[0]["payload"]["response"] == "Hello there"
    assert end_events[0]["payload"]["tokens_out"] == 7


@pytest.mark.asyncio
async def test_stream_chat_sdk_error_maps_to_llm_client_error() -> None:
    captured: dict[str, Any] = {}
    factory = _factory_for(captured, None, error=lmstudio.LMStudioServerError("boom"))
    client = LMStudioSDKClient(_settings(), client_factory=factory)
    with pytest.raises(LLMClientError, match="SDK stream failed"):
        async for _ in await client.stream_chat([{"role": "user", "content": "go"}]):
            pass
