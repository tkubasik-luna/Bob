"""LM Studio inference via the official ``lmstudio`` SDK (PRD 0017 / M2).

:class:`LMStudioSDKClient` is the SDK-transport counterpart to
:class:`bob.llm_client.LMStudioClient` (the OpenAI-compatible transport). It
implements the same :class:`bob.llm_client.LLMClient` interface but speaks to
LM Studio through the native websocket SDK
(``AsyncClient`` → ``client.llm.model(key)`` → ``model.respond(...)``) instead
of the OpenAI HTTP shim. The two are selected by ``LLM_LMSTUDIO_TRANSPORT`` in
:mod:`bob.llm.factory` (default ``openai`` — nothing changes until flipped).

Issue 0111 is the foundational tracer-bullet slice: only :meth:`chat` (+ the
guided-JSON structured-output mapping) goes through the SDK. ``complete`` /
``stream_chat`` / ``stream_complete`` and the long-lived per-role lifecycle land
in issues 0112-0115; until then this client raises a clear error for those
methods so the only supported ``sdk`` path is the POC ``chat()``. The flag
defaults to ``openai`` so production is never built on this partial client.

Observability is preserved EXACTLY like :class:`LMStudioClient.chat`: paired
``llm_call_start`` / ``llm_call_end`` debug events (same category / source style
/ payload keys), :func:`log_llm_call`, the empty-content guard, and real token
counts (here read from the SDK's prediction ``stats`` rather than the OpenAI
``usage`` block). SDK ``LMStudioError`` failures map to :class:`LLMClientError`.

The SDK boundary is faked in tests via the module-level
:func:`_default_async_client_factory` seam (mirrors
:func:`bob.lm_studio_manager._default_client_factory`): a constructor-injected
``client_factory`` lets the offline suite drive a scripted fake ``AsyncClient``
without a running LM Studio server. Downstream issues reuse this same seam.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import lmstudio
from lmstudio import AsyncClient, LlmPredictionConfigDict

from bob.config import Settings
from bob.debug_log import emit_debug
from bob.llm.lmstudio_sdk.history import messages_to_chat
from bob.llm.types import LLMResponse, ToolDefinition
from bob.llm_client import (
    LLMClient,
    LLMClientError,
    _assert_standard_roles,
    _estimate_tokens,
    _normalise_validator_role,
)
from bob.lm_studio_manager import host_from_base_url
from bob.logging_setup import log_llm_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bob.llm.types import StreamChunk

#: Hardcoded generation cap, matching :class:`LMStudioClient`'s ``max_tokens=4096``
#: on every request — kept identical so the two transports produce comparable
#: completions during the side-by-side period.
_MAX_TOKENS = 4096

#: Factory type for the SDK async client. The default builds a real
#: ``lmstudio.AsyncClient``; tests inject a fake to stay offline (mirrors
#: :data:`bob.lm_studio_manager.ClientFactory`).
AsyncClientFactory = Callable[[str], AsyncClient]


def _default_async_client_factory(host: str) -> AsyncClient:
    """Build a real ``lmstudio.AsyncClient`` pinned to ``host`` (``host:port``)."""

    return AsyncClient(host)


def schema_to_response_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Map Bob's guided-JSON ``schema`` to the SDK ``response_format`` shape.

    Bob passes the OpenAI ``json_schema`` wrapper ``{"name", "schema": {...}}``
    (see :func:`bob.thinker_loop.thinker_snapshot_response_schema`). The SDK's
    structured-output setting is ``{"type": "json", "jsonSchema": <json-schema>}``
    (``LlmStructuredPredictionSettingDict``). We unwrap the inner ``schema``
    payload — exactly like :class:`bob.llm_client.ClaudeCliClient` does with
    ``schema.get("schema", schema)`` — so the SDK token-gates the decode to the
    same grammar the OpenAI transport gated against.

    Kept a small pure function so the mapping is unit-testable in isolation.
    """

    json_schema = schema.get("schema", schema)
    return {"type": "json", "jsonSchema": json_schema}


class LMStudioSDKClient(LLMClient):
    """:class:`LLMClient` over the ``lmstudio`` SDK websocket transport (PRD 0017).

    Constructed exactly like :class:`bob.llm_client.LMStudioClient` — same
    ``settings`` + optional per-role ``model`` / ``reasoning`` overrides — so the
    factory can swap one for the other behind ``LLM_LMSTUDIO_TRANSPORT`` with no
    call-site change. The SDK host is derived from ``settings.LLM_BASE_URL`` via
    :func:`host_from_base_url` (single config source, shared with the manager).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        reasoning: str | None = None,
        client_factory: AsyncClientFactory | None = None,
    ) -> None:
        self._settings = settings
        # Per-role model routing (PRD 0016 / issue 0106): a role-built client
        # pins its own model via ``model``; ``None`` falls back to the frozen
        # ``settings.LLM_MODEL``. Same contract as ``LMStudioClient._model``.
        self._model_override = model
        # Per-role reasoning level (``"off"|"low"|"medium"|"high"|"on"``).
        # Forwarded via the SDK's raw-config passthrough (``config.raw``), the
        # SDK equivalent of the OpenAI transport's ``extra_body.reasoning``.
        # ``None`` omits it so the model's auto-chosen setting applies.
        self._reasoning = reasoning
        # DI seam: tests inject a fake ``AsyncClient`` factory to stay offline.
        self._client_factory = client_factory or _default_async_client_factory

    @property
    def _model(self) -> str | None:
        """The effective model id (per-role override, else ``.env``).

        Mirrors :attr:`bob.llm_client.LMStudioClient._model` so observability
        payloads report the role's model, not the global ``LLM_MODEL``.
        """

        return self._model_override or self._settings.LLM_MODEL

    @property
    def _host(self) -> str:
        """The SDK ``host:port`` derived from the (possibly per-role) base URL."""

        return host_from_base_url(self._settings.LLM_BASE_URL)

    def _build_config(self) -> LlmPredictionConfigDict:
        """Build the SDK ``LlmPredictionConfigDict`` for a request.

        ``maxTokens`` matches the OpenAI transport's hardcoded 4096 cap (the SDK
        config dict is camelCase — ``maxTokens`` / ``raw`` — unlike the snake-case
        dataclass form).

        The per-role ``reasoning`` level rides on the SDK raw-config passthrough
        — the SDK analogue of the OpenAI ``extra_body.reasoning`` field. NOTE
        (deviation from the investigation doc): the SDK's ``raw`` field is NOT a
        free-form ``{"reasoning": level}`` dict but a structured ``KvConfig``
        (``{"fields": [{"key", "value"}]}``), so the level is carried as a single
        ``reasoning`` KvConfig field. The exact key the LM Studio server honours
        for the reasoning effort is validated manually at the POC (the
        investigation flagged this mapping as OPEN); ``None`` omits ``raw``
        entirely so the model picks its own setting (documented fallback) and the
        migration is never blocked on it.
        """

        config: LlmPredictionConfigDict = {"maxTokens": _MAX_TOKENS}
        if self._reasoning is not None:
            config["raw"] = {"fields": [{"key": "reasoning", "value": self._reasoning}]}
        return config

    def supports_guided_json(self) -> bool:
        """LM Studio SDK gates ``chat(schema=…)`` via native structured output.

        The SDK maps the schema onto ``response_format`` (``{"type": "json",
        "jsonSchema": …}``), a real grammar gate — so the sub-agent runner can
        constrain its envelope under guided decoding, identical to the OpenAI
        transport. Returns ``True`` (PRD 0017 decision Q5).
        """

        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        # Issue 0048 — fold ``system_validator`` rows into prefixed ``system``
        # messages BEFORE conversion, then assert standard roles. Reuse the
        # OpenAI client's helpers (single source of the contract) so both
        # transports apply the identical fold + guard.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=False)
        _assert_standard_roles(messages)

        chat = messages_to_chat(messages)
        response_format = schema_to_response_format(schema) if schema is not None else None
        config = self._build_config()

        correlation_id = uuid4().hex
        token_estimate = _estimate_tokens(messages)
        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm.lmstudio_sdk.chat",
            summary=(f"LLM call démarré ({token_estimate} tokens prompt, model={self._model})"),
            payload={
                "messages": messages,
                "model": self._model,
                "reasoning": self._reasoning,
                "tokens_prompt_estimate": token_estimate,
                "has_schema": schema is not None,
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        started = time.perf_counter()
        try:
            result = await self._respond(chat, response_format=response_format, config=config)
        except lmstudio.LMStudioError as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm.lmstudio_sdk.chat",
                summary=f"LLM call échoué en {latency_ms:.0f}ms: {exc}",
                payload={
                    "model": self._model,
                    "latency_ms": latency_ms,
                    "exception": str(exc),
                    "exception_type": exc.__class__.__name__,
                    "traceback": traceback.format_exc(),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            # Map SDK errors onto the legacy ``LLMClientError`` so the existing
            # retry / degrade paths catch them unchanged.
            raise LLMClientError(f"LM Studio SDK call failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        raw = self._read_content(result)
        if not raw.strip():
            # Empty content slips through as ``""``; the sub-agent runner then
            # tries ``json.loads("")`` and surfaces a misleading "invalid JSON".
            # Detect early + emit a greppable event (warmup, context overflow,
            # abrupt abort) — same guard as :class:`LMStudioClient.chat`.
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm.lmstudio_sdk.chat",
                summary=(f"LLM call returned empty content ({latency_ms:.0f}ms)"),
                payload={
                    "model": self._model,
                    "host": self._host,
                    "latency_ms": latency_ms,
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(
                "LM Studio SDK returned empty content. "
                "Check the model is loaded and the prompt fits the context window."
            )

        tokens_in, tokens_out = self._read_tokens(result)

        log_llm_call(
            session_id=session_id,
            messages=messages,
            raw_response=raw,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm.lmstudio_sdk.chat",
            summary=(
                f"LLM call terminé en {latency_ms:.0f}ms "
                f"({tokens_out if tokens_out is not None else '?'} tokens response)"
            ),
            payload={
                "response": raw,
                "latency_ms": latency_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": self._model,
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        return raw

    async def _respond(
        self,
        chat: Any,
        *,
        response_format: dict[str, Any] | None,
        config: LlmPredictionConfigDict,
    ) -> Any:
        """Obtain a model handle and run one ``respond`` against ``chat``.

        Builds a short-lived :class:`AsyncClient` for the role's host, resolves
        the model handle (``client.llm.model(key)``) and calls ``model.respond``.
        The long-lived per-role client lifecycle (PRD 0017 / M7, issue 0115)
        replaces this per-call open; for the POC a per-call client is correct
        and keeps the seam trivially fakeable. The whole body runs inside the
        ``async with`` so the websocket is always closed.
        """

        async with self._client_factory(self._host) as client:
            model = await client.llm.model(self._model)
            return await model.respond(
                chat,
                response_format=response_format,
                config=config,
            )

    @staticmethod
    def _read_content(result: Any) -> str:
        """Read the response text from a ``PredictionResult``.

        Prefers ``.content`` (the model's text). Falls back to ``.parsed`` for a
        structured-output result whose ``.content`` may be empty — coerced to a
        string so the empty-content guard and the action parser see a string.
        """

        content = getattr(result, "content", None)
        if isinstance(content, str) and content:
            return content
        parsed = getattr(result, "parsed", None)
        if parsed is None:
            return content if isinstance(content, str) else ""
        if isinstance(parsed, str):
            return parsed
        import json

        try:
            return json.dumps(parsed, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(parsed)

    @staticmethod
    def _read_tokens(result: Any) -> tuple[int | None, int | None]:
        """Read prompt / completion token counts from the SDK prediction stats.

        The SDK reports real counts on ``result.stats`` (``LlmPredictionStats``)
        — ``prompt_tokens_count`` / ``predicted_tokens_count`` — replacing the
        OpenAI ``usage.prompt_tokens`` / ``completion_tokens`` reads. Missing
        stats (or fields) collapse to ``None`` so observability degrades softly.
        """

        stats = getattr(result, "stats", None)
        if stats is None:
            return None, None
        tokens_in = getattr(stats, "prompt_tokens_count", None)
        tokens_out = getattr(stats, "predicted_tokens_count", None)
        return tokens_in, tokens_out

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        """Not implemented in issue 0111 — the SDK tool-calling path lands in 0113.

        The ``sdk`` transport is a ``chat()``-only POC at this slice. Raising a
        clear :class:`LLMClientError` (rather than silently degrading) makes a
        premature ``complete`` under ``LLM_LMSTUDIO_TRANSPORT=sdk`` fail loudly;
        the flag defaults to ``openai`` so no production path reaches here.
        """

        raise LLMClientError(
            "LMStudioSDKClient.complete() is not implemented yet — the SDK "
            "tool-calling transport arrives in issue 0113. Use "
            "LLM_LMSTUDIO_TRANSPORT=openai for tool-calling until then."
        )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming guided-JSON ``chat`` over the SDK transport (PRD 0017 / M5).

        The SDK-transport counterpart of
        :meth:`bob.llm_client.LMStudioClient.stream_chat`: same validator-role
        fold, same ``messages_to_chat`` conversion, same ``response_format`` when
        ``schema`` is set and the same ``config.raw`` reasoning passthrough as
        :meth:`chat`, then drives ``model.respond_stream(...)`` and adapts the
        SDK fragment stream into Bob :class:`StreamChunk`s via module M5
        (:func:`bob.llm.lmstudio_sdk.streaming.adapt_prediction_stream`).

        Yields ``text`` + ``reasoning`` chunks tick by tick, then one terminal
        ``perf`` chunk built from the SDK's :class:`lmstudio.LlmPredictionStats`.
        Concatenating the ``text`` deltas reconstructs BYTE-IDENTICALLY what
        :meth:`chat` returns for the same input (the sub-agent action parse is
        unchanged); ``reasoning`` deltas are cosmetic and never feed parsing
        (issue 0069). ``log_llm_call`` + the end debug event (aggregated text,
        tokens) are emitted in a ``finally``, exactly like the OpenAI transport's
        :meth:`bob.llm_client.LMStudioClient._consume_chat_stream`.

        This is an async generator: opening the (per-call) ``AsyncClient`` and
        driving ``respond_stream`` happen inside the body so the websocket stays
        open across iteration and is always closed via ``async with`` when the
        consumer drains (or abandons) the stream.
        """

        messages = _normalise_validator_role(messages, allow_arbitrary_roles=False)
        _assert_standard_roles(messages)

        chat = messages_to_chat(messages)
        response_format = schema_to_response_format(schema) if schema is not None else None
        config = self._build_config()

        correlation_id = uuid4().hex
        token_estimate = _estimate_tokens(messages)
        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm.lmstudio_sdk.stream_chat",
            summary=(
                f"LLM chat stream démarré ({token_estimate} tokens prompt, model={self._model})"
            ),
            payload={
                "messages": messages,
                "model": self._model,
                "reasoning": self._reasoning,
                "tokens_prompt_estimate": token_estimate,
                "has_schema": schema is not None,
                "session_id": session_id,
                "streaming": True,
            },
            correlation_id=correlation_id,
        )

        return self._consume_chat_stream(
            chat,
            response_format=response_format,
            config=config,
            session_id=session_id,
            correlation_id=correlation_id,
            messages=messages,
        )

    async def _consume_chat_stream(
        self,
        chat: Any,
        *,
        response_format: dict[str, Any] | None,
        config: LlmPredictionConfigDict,
        session_id: str | None,
        correlation_id: str,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[StreamChunk]:
        """Open the SDK stream and adapt it to ``StreamChunk``s (M5 driver).

        Mirrors :meth:`bob.llm_client.LMStudioClient._consume_chat_stream`: walk
        the stream yielding ``text`` / ``reasoning`` chunks (accumulating the text
        for the post-stream log), end with a terminal ``perf`` chunk, and always
        run ``log_llm_call`` + the end debug event in a ``finally``. SDK
        ``LMStudioError`` failures map to :class:`LLMClientError` (a start-failure
        debug event mirrors the ``chat()`` error arm).

        The per-call ``AsyncClient`` lifecycle (``async with``) wraps the whole
        iteration so the websocket is closed when the consumer finishes; the
        long-lived per-role client lands in issue 0115.
        """

        from bob.llm.lmstudio_sdk.streaming import adapt_prediction_stream

        text_buffer = ""
        tokens_in: int | None = None
        tokens_out: int | None = None
        started = time.perf_counter()

        def _accumulate(delta: str) -> None:
            nonlocal text_buffer
            text_buffer += delta

        try:
            async with self._client_factory(self._host) as client:
                model = await client.llm.model(self._model)
                try:
                    stream = await model.respond_stream(
                        chat,
                        response_format=response_format,
                        config=config,
                    )
                except lmstudio.LMStudioError as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    emit_debug(
                        category="llm",
                        severity="error",
                        source="bob.llm.lmstudio_sdk.stream_chat",
                        summary=f"LLM chat stream échoué en {latency_ms:.0f}ms: {exc}",
                        payload={
                            "model": self._model,
                            "latency_ms": latency_ms,
                            "exception": str(exc),
                            "exception_type": exc.__class__.__name__,
                            "traceback": traceback.format_exc(),
                            "session_id": session_id,
                        },
                        correlation_id=correlation_id,
                    )
                    raise LLMClientError(f"LM Studio SDK stream failed: {exc}") from exc

                async for chunk in adapt_prediction_stream(
                    aiter(stream),
                    stats_getter=lambda: getattr(stream, "stats", None),
                    started=started,
                    on_text=_accumulate,
                ):
                    if chunk.kind == "perf":
                        tokens_in = chunk.tokens_in
                        tokens_out = chunk.tokens_out
                    yield chunk
        finally:
            latency_ms = (time.perf_counter() - started) * 1000.0
            log_llm_call(
                session_id=session_id,
                messages=messages,
                raw_response=text_buffer,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            emit_debug(
                category="llm",
                severity="info",
                source="bob.llm.lmstudio_sdk.stream_chat",
                summary=(
                    f"LLM chat stream terminé en {latency_ms:.0f}ms "
                    f"({tokens_out if tokens_out is not None else '?'} tokens response)"
                ),
                payload={
                    "response": text_buffer,
                    "latency_ms": latency_ms,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": self._model,
                    "session_id": session_id,
                    "streaming": True,
                },
                correlation_id=correlation_id,
            )

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Not implemented in issue 0111 — SDK streaming tool-calls land in 0114.

        Same rationale as :meth:`stream_chat`: fail loudly rather than replay the
        unimplemented :meth:`complete` as a degraded single-chunk stream.
        """

        raise LLMClientError(
            "LMStudioSDKClient.stream_complete() is not implemented yet — SDK "
            "streaming tool-calls arrive in issue 0114. Use "
            "LLM_LMSTUDIO_TRANSPORT=openai for streaming tool-calls until then."
        )


__all__ = ["LMStudioSDKClient", "schema_to_response_format"]
