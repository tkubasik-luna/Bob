"""LLM client abstraction and LM Studio / Claude CLI implementations.

The abstract :class:`LLMClient` is intentionally tiny — a ``chat`` method
returning the raw string emitted by the model and a ``complete`` method
exposing the OpenAI-compatible tool-calling surface. Higher layers
(:mod:`bob.orchestrator`, :mod:`bob.sub_agent.runner`, the validation
retry path in :mod:`bob.validation`) take care of schema enforcement,
retry budgets and degrade fallbacks. Pre-0048 the
``bob.response_parser`` module also lived in that higher layer; it was
deleted in 0048 because the silent raw-text fallback amounted to assistant-
history corruption.

Issue 0048 adds the ``system_validator`` role contract. Each client
normalises ``system_validator`` rows before dispatching:

- :class:`LMStudioClient` passes them through verbatim (OpenAI-compatible
  endpoints tolerate arbitrary role strings).
- :class:`ClaudeCliClient` folds them into ``system`` rows prefixed with
  :data:`bob.validation.system_validator.FALLBACK_VALIDATOR_PREFIX`
  because the CLI's prompt rendering only understands the four standard
  roles.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

import structlog
from openai import AsyncOpenAI

from bob.config import Settings
from bob.debug_log import emit_debug
from bob.llm.types import LLMResponse, StreamChunk, ToolCall, ToolDefinition
from bob.logging_setup import log_llm_call
from bob.validation.system_validator import (
    FALLBACK_VALIDATOR_PREFIX,
    SYSTEM_VALIDATOR_ROLE,
)

_logger = structlog.get_logger(__name__)


#: Roles known to be accepted by every OpenAI-compatible endpoint Bob
#: targets (LM Studio, vLLM, llama.cpp's server, Claude CLI in tool
#: mode). When the validation path hands us a message with a role
#: outside this set we either pass it through (LM Studio: arbitrary role
#: strings are accepted) or wrap it into a ``system`` message prefixed
#: with :data:`FALLBACK_VALIDATOR_PREFIX`. Issue 0048 — the wrap path is
#: the safety net documented in :mod:`bob.validation.system_validator`.
_STANDARD_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


def _normalise_validator_role(
    messages: list[dict[str, Any]],
    *,
    allow_arbitrary_roles: bool,
) -> list[dict[str, Any]]:
    """Return ``messages`` with ``system_validator`` rows handled.

    When ``allow_arbitrary_roles`` is true the messages are returned as
    is (the upstream provider accepts custom roles). Otherwise each
    ``system_validator`` row is folded into a ``system`` message
    prefixed with :data:`FALLBACK_VALIDATOR_PREFIX` so the validator
    payload stays distinguishable from a real system prompt.

    The function is a no-op when no ``system_validator`` messages are
    present so production calls pay zero overhead on the happy path.
    """

    if allow_arbitrary_roles:
        return messages
    if not any(msg.get("role") == SYSTEM_VALIDATOR_ROLE for msg in messages):
        return messages
    normalised: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != SYSTEM_VALIDATOR_ROLE:
            normalised.append(msg)
            continue
        normalised.append(
            {
                "role": "system",
                "content": FALLBACK_VALIDATOR_PREFIX + str(msg.get("content", "")),
            }
        )
    return normalised


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough heuristic for the prompt-side token count.

    Used in the ``llm_call_start`` debug summary before the API responds with
    a real token count. We approximate at ~4 chars per token (the rule of
    thumb for English; French is in the same ballpark) so the summary line
    has a number to anchor latency expectations. The real token counts land
    on the ``llm_call_end`` event from the provider's ``usage`` field.
    """

    total_chars = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif content is not None:
            total_chars += len(str(content))
    return total_chars // 4


class LLMClientError(RuntimeError):
    """Raised when an LLM backend fails irrecoverably (non-zero exit, timeout)."""


def _strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence around a JSON payload.

    Some chat models — notably ``claude -p`` — like to wrap JSON in
    ```json ... ``` even when told not to. Returns ``text`` untouched if no
    fence is detected.
    """

    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    first = lines[0].lstrip("`").strip().lower()
    if first not in ("", "json"):
        return text
    body_end = len(lines)
    if lines[-1].strip().startswith("```"):
        body_end -= 1
    return "\n".join(lines[1:body_end]).strip()


class LLMClient(ABC):
    """Abstract interface for an OpenAI-compatible chat LLM."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Send ``messages`` to the LLM and return the raw response string.

        If ``schema`` is provided, ask the backend for a JSON response matching
        the given JSON Schema (LM Studio's structured output feature).
        ``session_id`` is purely passthrough for the call-log file — no business
        logic depends on it at this layer.
        """

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        """Send ``messages`` to the LLM and return either text or tool calls.

        When ``tools`` is supplied the backend is told the model may call any of
        them. The return value is an :class:`LLMResponse`:

        - ``text != None`` and ``tool_calls == []`` → the model answered with
          plain text. This is allowed even when ``tools`` was non-empty (the
          model just chose not to call anything).
        - ``text is None`` and ``tool_calls`` non-empty → the model wants to
          invoke one or more tools.

        Raises :class:`LLMClientError` if the backend returns a structurally
        invalid response (e.g. tool-call arguments that are not valid JSON).
        """

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a tool-call (or plain text) response chunk-by-chunk.

        PRD 0006 / issue 0049. The orchestrator uses this to pipe
        ``delta.tool_calls[0].function.arguments`` bytes into a
        :class:`bob.streaming.StreamEmitter` while the LLM is still
        generating — so the user hears Jarvis start speaking almost
        immediately.

        Default implementation runs the existing :meth:`complete` and
        replays the final result as a single
        (``tool_call_start`` + ``tool_call_end``) pair so providers that
        don't implement native streaming (Claude CLI) still satisfy the
        contract. Tests can substitute a :class:`FakeLLMClient` that
        scripts the chunk sequence directly.

        The return type is ``AsyncIterator[StreamChunk]`` — call sites
        consume with ``async for``. The method itself is ``async def``
        because some implementations (LM Studio) need to await the
        underlying HTTP open before yielding the first chunk.
        """

        response = await self.complete(messages, tools=tools, session_id=session_id)
        return self._fallback_stream(response)

    @staticmethod
    async def _fallback_stream(response: LLMResponse) -> AsyncIterator[StreamChunk]:
        """Synthesise a chunk sequence for providers without native streaming.

        Yields a (``tool_call_start``, ``tool_call_args_delta``,
        ``tool_call_end``) trio per tool call, in order, so the
        :class:`bob.streaming.StreamEmitter` sees the same surface as
        the LM Studio streaming path. The ``args_delta`` carries the
        FULL argument JSON (not a partial slice) because the upstream
        client already parsed and re-serialised the call.
        """

        if response.tool_calls:
            for call in response.tool_calls:
                arguments_str = json.dumps(call.arguments, ensure_ascii=False)
                yield StreamChunk(
                    kind="tool_call_start",
                    tool_call_id=call.id,
                    name=call.name,
                )
                if arguments_str:
                    yield StreamChunk(
                        kind="tool_call_args_delta",
                        tool_call_id=call.id,
                        args_delta=arguments_str,
                    )
                yield StreamChunk(
                    kind="tool_call_end",
                    tool_call_id=call.id,
                    final_arguments=call.arguments,
                )
            return

        if response.text is not None:
            yield StreamChunk(kind="text", text_delta=response.text)


class LMStudioClient(LLMClient):
    """:class:`LLMClient` implementation wrapping ``openai.AsyncOpenAI``.

    Configured to talk to a local LM Studio instance (or any OpenAI-compatible
    endpoint) via :class:`Settings`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        # Issue 0048 — pass ``system_validator`` rows straight through.
        # LM Studio (and every OpenAI-compatible endpoint Bob targets)
        # tolerates arbitrary role strings on chat messages. The fold
        # path in :func:`_normalise_validator_role` exists for upstream
        # providers that reject unknown roles; we opt in to it only on
        # the Claude CLI client below.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=True)
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": 4096,
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": schema,
            }

        # Slice 0039: pair start / end debug events for this call.
        correlation_id = uuid4().hex
        token_estimate = _estimate_tokens(messages)
        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm_client.chat",
            summary=(
                f"LLM call démarré ({token_estimate} tokens prompt, "
                f"model={self._settings.LLM_MODEL})"
            ),
            payload={
                "messages": messages,
                "model": self._settings.LLM_MODEL,
                "tokens_prompt_estimate": token_estimate,
                "has_schema": schema is not None,
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        started = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.chat",
                summary=f"LLM call échoué en {latency_ms:.0f}ms: {exc}",
                payload={
                    "model": self._settings.LLM_MODEL,
                    "latency_ms": latency_ms,
                    "exception": str(exc),
                    "exception_type": exc.__class__.__name__,
                    "traceback": traceback.format_exc(),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0

        choices = getattr(completion, "choices", None)
        if not choices:
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.chat",
                summary=f"LLM call returned no choices ({latency_ms:.0f}ms)",
                payload={
                    "model": self._settings.LLM_MODEL,
                    "base_url": self._settings.LLM_BASE_URL,
                    "latency_ms": latency_ms,
                    "raw_completion": completion.model_dump()
                    if hasattr(completion, "model_dump")
                    else repr(completion),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(
                "LLM endpoint returned no choices — response is not OpenAI-compatible. "
                f"Check LLM_BASE_URL={self._settings.LLM_BASE_URL!r} "
                "(LM Studio expects the '/v1' suffix, e.g. http://host:1234/v1)."
            )
        message = choices[0].message
        content = message.content or ""
        if not content:
            content = getattr(message, "reasoning_content", "") or ""
        raw = cast(str, content)

        # Empty content slips through as ``""``; sub-agent runner then
        # tries ``json.loads("")`` and surfaces a misleading
        # "invalid JSON" error. Detect early + emit a structured event
        # so the failure mode is greppable (model warmup, context
        # overflow, abrupt provider abort).
        if not raw.strip():
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.chat",
                summary=(
                    f"LLM call returned empty content ({latency_ms:.0f}ms)"
                ),
                payload={
                    "model": self._settings.LLM_MODEL,
                    "base_url": self._settings.LLM_BASE_URL,
                    "latency_ms": latency_ms,
                    "finish_reason": getattr(choices[0], "finish_reason", None),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(
                "LLM endpoint returned empty content. "
                f"finish_reason={getattr(choices[0], 'finish_reason', None)!r}. "
                "Check the model is loaded and the prompt fits the context window."
            )

        tokens_in: int | None = None
        tokens_out: int | None = None
        usage = getattr(completion, "usage", None)
        if usage is not None:
            tokens_in = getattr(usage, "prompt_tokens", None)
            tokens_out = getattr(usage, "completion_tokens", None)

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
            source="bob.llm_client.chat",
            summary=(
                f"LLM call terminé en {latency_ms:.0f}ms "
                f"({tokens_out if tokens_out is not None else '?'} tokens response)"
            ),
            payload={
                "response": raw,
                "latency_ms": latency_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": self._settings.LLM_MODEL,
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        return raw

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        # Issue 0048 — same passthrough policy as ``chat``: LM Studio
        # accepts the ``system_validator`` role verbatim. See
        # :func:`_normalise_validator_role` for the rationale.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=True)
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": 4096,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            kwargs["tool_choice"] = "auto"

        # Slice 0039: pair start / end debug events via a local correlation_id
        # so the UI can group them. The id is regenerated per call (no cross-
        # call leakage); ``turn_id`` propagates automatically through the
        # ``current_turn_id`` ContextVar set by ``Orchestrator.process_user_message``.
        correlation_id = uuid4().hex
        token_estimate = _estimate_tokens(messages)
        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm_client.complete",
            summary=(
                f"LLM call démarré ({token_estimate} tokens prompt, "
                f"model={self._settings.LLM_MODEL})"
            ),
            payload={
                "messages": messages,
                "model": self._settings.LLM_MODEL,
                "tokens_prompt_estimate": token_estimate,
                "has_tools": bool(tools),
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        started = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.complete",
                summary=f"LLM call échoué en {latency_ms:.0f}ms: {exc}",
                payload={
                    "model": self._settings.LLM_MODEL,
                    "latency_ms": latency_ms,
                    "exception": str(exc),
                    "exception_type": exc.__class__.__name__,
                    "traceback": traceback.format_exc(),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0

        choices = getattr(completion, "choices", None)
        if not choices:
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.complete",
                summary=f"LLM call returned no choices ({latency_ms:.0f}ms)",
                payload={
                    "model": self._settings.LLM_MODEL,
                    "base_url": self._settings.LLM_BASE_URL,
                    "latency_ms": latency_ms,
                    "raw_completion": completion.model_dump()
                    if hasattr(completion, "model_dump")
                    else repr(completion),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(
                "LLM endpoint returned no choices — response is not OpenAI-compatible. "
                f"Check LLM_BASE_URL={self._settings.LLM_BASE_URL!r} "
                "(LM Studio expects the '/v1' suffix, e.g. http://host:1234/v1)."
            )
        message = choices[0].message
        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls: list[ToolCall] = []
        for raw_call in raw_tool_calls:
            function = getattr(raw_call, "function", None)
            if function is None:
                continue
            name = getattr(function, "name", None) or ""
            arguments_raw = getattr(function, "arguments", "") or ""
            try:
                arguments = json.loads(arguments_raw) if arguments_raw else {}
            except json.JSONDecodeError as exc:
                emit_debug(
                    category="llm",
                    severity="error",
                    source="bob.llm_client.complete",
                    summary=f"LLM call malformed tool args ({latency_ms:.0f}ms)",
                    payload={
                        "model": self._settings.LLM_MODEL,
                        "latency_ms": latency_ms,
                        "arguments_raw": arguments_raw,
                        "exception": str(exc),
                        "session_id": session_id,
                    },
                    correlation_id=correlation_id,
                )
                raise LLMClientError(
                    f"LM Studio tool call arguments are not valid JSON: {arguments_raw[:200]!r}"
                ) from exc
            if not isinstance(arguments, dict):
                raise LLMClientError(
                    f"LM Studio tool call arguments must decode to an object, "
                    f"got {type(arguments).__name__}"
                )
            call_id = getattr(raw_call, "id", None) or f"call_{uuid4().hex[:8]}"
            tool_calls.append(
                ToolCall(id=call_id, name=name, arguments=cast(dict[str, Any], arguments))
            )

        text: str | None
        if tool_calls:
            text = None
            raw_for_log = json.dumps(
                [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls],
                ensure_ascii=False,
            )
        else:
            content = message.content or ""
            if not content:
                content = getattr(message, "reasoning_content", "") or ""
            text_value = cast(str, content)
            if not text_value:
                emit_debug(
                    category="llm",
                    severity="error",
                    source="bob.llm_client.complete",
                    summary=f"LLM call empty response ({latency_ms:.0f}ms)",
                    payload={
                        "model": self._settings.LLM_MODEL,
                        "latency_ms": latency_ms,
                        "session_id": session_id,
                    },
                    correlation_id=correlation_id,
                )
                raise LLMClientError("LM Studio returned empty response")
            text = text_value
            raw_for_log = text_value

        tokens_in: int | None = None
        tokens_out: int | None = None
        usage = getattr(completion, "usage", None)
        if usage is not None:
            tokens_in = getattr(usage, "prompt_tokens", None)
            tokens_out = getattr(usage, "completion_tokens", None)

        log_llm_call(
            session_id=session_id,
            messages=messages,
            raw_response=raw_for_log,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm_client.complete",
            summary=(
                f"LLM call terminé en {latency_ms:.0f}ms "
                f"({tokens_out if tokens_out is not None else '?'} tokens response)"
            ),
            payload={
                "response": raw_for_log,
                "is_tool_call": bool(tool_calls),
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
                ],
                "latency_ms": latency_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": self._settings.LLM_MODEL,
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        return LLMResponse(text=text, tool_calls=tool_calls)

    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming counterpart to :meth:`complete` (PRD 0006 / issue 0049).

        Drives an OpenAI-compatible streaming chat-completion request
        (``stream=True``) and yields one :class:`StreamChunk` per
        provider tick. The chunk lifecycle is:

        1. The first time we see a ``tool_calls`` delta with a
           non-empty ``function.name``, we emit a ``tool_call_start``
           with the resolved id + name. Provider-assigned ids carry
           through verbatim; missing ids get a deterministic
           ``call_<8-hex>`` placeholder (matches the legacy
           :meth:`complete` behaviour).
        2. Every subsequent argument-bytes delta becomes a
           ``tool_call_args_delta`` with the new suffix. We accumulate
           the suffix locally so :class:`bob.streaming.StreamEmitter`
           can re-parse the buffer without owning the underlying byte
           stream.
        3. When the provider closes the stream we emit one
           ``tool_call_end`` per tool call, parsing the accumulated
           argument JSON in the process. A malformed final JSON raises
           :class:`LLMClientError` (matches :meth:`complete` — the
           orchestrator's retry path catches it).
        4. If the model emitted plain text instead of a tool call, we
           emit ``text`` chunks instead. Text mode is uncommon under
           the unified ``say`` tool but supported for robustness.

        Debug events follow the same ``llm_call_start`` /
        ``llm_call_end`` pairing as :meth:`complete`. ``stream=True``
        means we don't know the prompt token count up front the same
        way; the post-stream ``usage`` field (when surfaced by the
        provider) lands on the end event.
        """

        # Issue 0048 — same passthrough policy as ``complete``.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=True)
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": 4096,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            kwargs["tool_choice"] = "auto"

        correlation_id = uuid4().hex
        token_estimate = _estimate_tokens(messages)
        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm_client.stream_complete",
            summary=(
                f"LLM stream démarré ({token_estimate} tokens prompt, "
                f"model={self._settings.LLM_MODEL})"
            ),
            payload={
                "messages": messages,
                "model": self._settings.LLM_MODEL,
                "tokens_prompt_estimate": token_estimate,
                "has_tools": bool(tools),
                "session_id": session_id,
                "streaming": True,
            },
            correlation_id=correlation_id,
        )

        started = time.perf_counter()
        # ``self._client.chat.completions.create`` returns either a
        # full response (stream=False) or an ``AsyncStream`` (stream=True).
        # We pass ``stream=True`` via kwargs so the SDK returns the
        # async iterator the SSE machinery is wrapped under.
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.stream_complete",
                summary=f"LLM stream échoué en {latency_ms:.0f}ms: {exc}",
                payload={
                    "model": self._settings.LLM_MODEL,
                    "latency_ms": latency_ms,
                    "exception": str(exc),
                    "exception_type": exc.__class__.__name__,
                    "traceback": traceback.format_exc(),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise

        return self._consume_stream(
            stream,
            session_id=session_id,
            correlation_id=correlation_id,
            messages=messages,
            started=started,
        )

    async def _consume_stream(
        self,
        stream: Any,
        *,
        session_id: str | None,
        correlation_id: str,
        messages: list[dict[str, Any]],
        started: float,
    ) -> AsyncIterator[StreamChunk]:
        """Walk the OpenAI ``AsyncStream`` and re-emit it as ``StreamChunk``s.

        Splitting this out of :meth:`stream_complete` keeps the
        per-tick logic separate from the request-setup code path and
        gives us a single function the test harness can drive with a
        scripted iterator.
        """

        # Track per-index tool-call state. The OpenAI streaming protocol
        # emits one or more ``choices[0].delta.tool_calls[i]`` entries
        # per chunk, where ``i`` is stable across the call. We
        # accumulate name + arguments per ``i`` and emit chunks lazily
        # as new bytes arrive.
        tool_call_states: dict[int, dict[str, Any]] = {}
        text_buffer = ""
        tokens_in: int | None = None
        tokens_out: int | None = None

        try:
            async for raw_chunk in stream:
                choices = getattr(raw_chunk, "choices", None) or []
                if not choices:
                    # OpenAI emits a closing ``usage`` chunk on some
                    # providers with no ``choices`` payload.
                    usage = getattr(raw_chunk, "usage", None)
                    if usage is not None:
                        tokens_in = getattr(usage, "prompt_tokens", None)
                        tokens_out = getattr(usage, "completion_tokens", None)
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                # Text-mode delta — uncommon under the unified ``say``
                # tool, supported for robustness.
                content = getattr(delta, "content", None) or ""
                if isinstance(content, str) and content:
                    text_buffer += content
                    yield StreamChunk(kind="text", text_delta=content)

                # Tool-call deltas. Each entry carries an ``index``,
                # an optional ``id`` (provider-assigned), and a
                # ``function`` with optional ``name`` + ``arguments``.
                raw_tool_calls = getattr(delta, "tool_calls", None) or []
                for raw_tc in raw_tool_calls:
                    index = getattr(raw_tc, "index", 0)
                    function = getattr(raw_tc, "function", None)
                    state = tool_call_states.setdefault(
                        index,
                        {
                            "id": getattr(raw_tc, "id", None),
                            "name": None,
                            "arguments": "",
                            "started_yielded": False,
                        },
                    )
                    # Provider-assigned id may arrive on the first or
                    # second tick depending on the upstream.
                    incoming_id = getattr(raw_tc, "id", None)
                    if incoming_id and not state["id"]:
                        state["id"] = incoming_id

                    if function is not None:
                        incoming_name = getattr(function, "name", None)
                        if incoming_name and not state["name"]:
                            state["name"] = incoming_name

                    # Emit the ``tool_call_start`` chunk the first time
                    # we have BOTH a resolved name and a resolved id.
                    # Without a name the orchestrator can't dispatch.
                    if not state["started_yielded"] and state["name"]:
                        if not state["id"]:
                            state["id"] = f"call_{uuid4().hex[:8]}"
                        state["started_yielded"] = True
                        yield StreamChunk(
                            kind="tool_call_start",
                            tool_call_id=cast(str, state["id"]),
                            name=cast(str, state["name"]),
                        )

                    if function is not None:
                        args_delta = getattr(function, "arguments", None) or ""
                        if isinstance(args_delta, str) and args_delta:
                            state["arguments"] = cast(str, state["arguments"]) + args_delta
                            # We can only emit ``tool_call_args_delta``
                            # once the start chunk has gone out — that
                            # invariant matches the
                            # :class:`bob.streaming.StreamEmitter`
                            # expectation that ``msg_id`` is bound on
                            # the very first frame of the turn.
                            if state["started_yielded"]:
                                yield StreamChunk(
                                    kind="tool_call_args_delta",
                                    tool_call_id=cast(str, state["id"]),
                                    args_delta=args_delta,
                                )

                # Final usage chunk on some providers.
                usage = getattr(raw_chunk, "usage", None)
                if usage is not None:
                    tokens_in = getattr(usage, "prompt_tokens", None) or tokens_in
                    tokens_out = getattr(usage, "completion_tokens", None) or tokens_out

            # Stream exhausted — emit ``tool_call_end`` per accumulated
            # tool call, parsing the final argument JSON.
            for state in tool_call_states.values():
                if not state["started_yielded"]:
                    # Defensive: the stream ended without resolving a
                    # name. Skip the end chunk — there is no tool to
                    # dispatch. The orchestrator's retry path will
                    # surface the contract violation.
                    continue
                arguments_raw = cast(str, state["arguments"])
                try:
                    final_arguments = json.loads(arguments_raw) if arguments_raw else {}
                except json.JSONDecodeError as exc:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    emit_debug(
                        category="llm",
                        severity="error",
                        source="bob.llm_client.stream_complete",
                        summary=(f"LLM stream malformed final args ({latency_ms:.0f}ms)"),
                        payload={
                            "model": self._settings.LLM_MODEL,
                            "latency_ms": latency_ms,
                            "arguments_raw": arguments_raw,
                            "exception": str(exc),
                            "session_id": session_id,
                        },
                        correlation_id=correlation_id,
                    )
                    raise LLMClientError(
                        "LM Studio tool call arguments are not valid JSON after "
                        f"stream close: {arguments_raw[:200]!r}"
                    ) from exc
                if not isinstance(final_arguments, dict):
                    raise LLMClientError(
                        "LM Studio tool call arguments must decode to an object, "
                        f"got {type(final_arguments).__name__}"
                    )
                yield StreamChunk(
                    kind="tool_call_end",
                    tool_call_id=cast(str, state["id"]),
                    final_arguments=cast(dict[str, Any], final_arguments),
                )
        finally:
            latency_ms = (time.perf_counter() - started) * 1000.0
            # Reconstruct a raw-for-log shape mirroring :meth:`complete`.
            log_calls = [
                {
                    "id": state["id"],
                    "name": state["name"],
                    "arguments": state["arguments"],
                }
                for state in tool_call_states.values()
                if state["started_yielded"]
            ]
            raw_for_log = json.dumps(log_calls, ensure_ascii=False) if log_calls else text_buffer

            log_llm_call(
                session_id=session_id,
                messages=messages,
                raw_response=raw_for_log,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

            emit_debug(
                category="llm",
                severity="info",
                source="bob.llm_client.stream_complete",
                summary=(
                    f"LLM stream terminé en {latency_ms:.0f}ms "
                    f"({tokens_out if tokens_out is not None else '?'} tokens response)"
                ),
                payload={
                    "response": raw_for_log,
                    "is_tool_call": bool(log_calls),
                    "tool_calls": [
                        {
                            "id": state["id"],
                            "name": state["name"],
                            "arguments": state["arguments"],
                        }
                        for state in tool_call_states.values()
                        if state["started_yielded"]
                    ],
                    "latency_ms": latency_ms,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": self._settings.LLM_MODEL,
                    "session_id": session_id,
                    "streaming": True,
                },
                correlation_id=correlation_id,
            )


class ClaudeCliClient(LLMClient):
    """:class:`LLMClient` implementation shelling out to the ``claude`` CLI.

    Each call spawns ``claude -p --output-format json`` and feeds the full
    conversation through ``--system-prompt`` + a serialized history on stdin.
    When a JSON schema is supplied it is appended to the system prompt as an
    instruction (the CLI's ``--json-schema`` flag only validates and silently
    drops invalid output, which would defeat the response-parser retry loop).
    Tools are disabled (``--tools ""``) since Bob only needs the chat reply.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def _split_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        rest: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(str(msg.get("content", "")))
            else:
                rest.append(msg)
        return "\n\n".join(system_parts), rest

    @staticmethod
    def _render_history(history: list[dict[str, Any]]) -> str:
        """Serialize non-system history into a single prompt string.

        ``claude -p`` consumes one prompt per invocation. For multi-turn
        contexts we render the prior exchanges as labeled blocks and put the
        latest user message at the end so the model sees it as the live turn.
        """

        if not history:
            return ""
        if len(history) == 1 and history[0].get("role") == "user":
            return str(history[0].get("content", ""))

        lines: list[str] = ["Conversation so far:"]
        for msg in history[:-1]:
            role = str(msg.get("role", "user")).upper()
            content = str(msg.get("content", ""))
            lines.append(f"[{role}]\n{content}")
        last = history[-1]
        last_role = str(last.get("role", "user")).upper()
        lines.append(f"\nCurrent [{last_role}] message:\n{last.get('content', '')}")
        return "\n\n".join(lines)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        # Issue 0048 — fold ``system_validator`` messages into ``system``
        # rows with a ``[VALIDATOR]:`` prefix. The Claude CLI consumes a
        # single rendered prompt; arbitrary roles don't survive
        # :meth:`_split_messages` / :meth:`_render_history` cleanly.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=False)
        system_prompt, history = self._split_messages(messages)
        prompt = self._render_history(history)

        if schema is not None:
            schema_payload = schema.get("schema", schema)
            schema_instruction = (
                "\n\nIMPORTANT: réponds UNIQUEMENT par un objet JSON valide, "
                "sans aucun texte avant ou après, SANS bloc de code markdown "
                "(pas de ```json ni ```), conforme à ce JSON Schema :\n"
                f"{json.dumps(schema_payload, ensure_ascii=False)}"
            )
            system_prompt = (system_prompt + schema_instruction).strip()

        argv: list[str] = [
            self._settings.CLAUDE_CLI_BIN,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
        ]
        if system_prompt:
            argv += ["--system-prompt", system_prompt]
        if self._settings.CLAUDE_CLI_MODEL:
            argv += ["--model", self._settings.CLAUDE_CLI_MODEL]

        _logger.info(
            "claude_cli.request",
            session_id=session_id,
            model=self._settings.CLAUDE_CLI_MODEL,
            history_len=len(history),
            prompt=prompt,
            system_prompt_chars=len(system_prompt),
            has_schema=schema is not None,
        )

        # Slice 0039: pair start / end debug events. Same pattern as
        # :class:`LMStudioClient`; ``turn_id`` is auto-filled from the
        # ContextVar.
        correlation_id = uuid4().hex
        token_estimate = _estimate_tokens(messages)
        emit_debug(
            category="llm",
            severity="info",
            source="bob.llm_client.chat",
            summary=(
                f"LLM call démarré ({token_estimate} tokens prompt, "
                f"model={self._settings.CLAUDE_CLI_MODEL or 'claude_cli'})"
            ),
            payload={
                "messages": messages,
                "model": self._settings.CLAUDE_CLI_MODEL,
                "tokens_prompt_estimate": token_estimate,
                "has_schema": schema is not None,
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self._settings.CLAUDE_CLI_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.chat",
                summary=f"LLM call timeout après {latency_ms:.0f}ms",
                payload={
                    "model": self._settings.CLAUDE_CLI_MODEL,
                    "latency_ms": latency_ms,
                    "timeout_seconds": self._settings.CLAUDE_CLI_TIMEOUT_SECONDS,
                    "exception": str(exc),
                    "traceback": traceback.format_exc(),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(
                f"claude CLI timed out after {self._settings.CLAUDE_CLI_TIMEOUT_SECONDS}s"
            ) from exc
        except FileNotFoundError as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.chat",
                summary=f"LLM call binary missing après {latency_ms:.0f}ms",
                payload={
                    "model": self._settings.CLAUDE_CLI_MODEL,
                    "latency_ms": latency_ms,
                    "exception": str(exc),
                    "traceback": traceback.format_exc(),
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(
                f"claude CLI binary not found: {self._settings.CLAUDE_CLI_BIN!r}"
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:500]
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.chat",
                summary=f"LLM call exit={proc.returncode} en {latency_ms:.0f}ms",
                payload={
                    "model": self._settings.CLAUDE_CLI_MODEL,
                    "latency_ms": latency_ms,
                    "return_code": proc.returncode,
                    "stderr": stderr_text,
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(f"claude CLI exited with code {proc.returncode}: {stderr_text}")

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raw, tokens_in, tokens_out, is_error = self._extract_result(stdout)

        _logger.info(
            "claude_cli.response",
            session_id=session_id,
            latency_ms=round(latency_ms, 2),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            is_error=is_error,
            result=raw,
            stderr=stderr if stderr.strip() else None,
        )

        if is_error:
            emit_debug(
                category="llm",
                severity="error",
                source="bob.llm_client.chat",
                summary=f"LLM call reported error en {latency_ms:.0f}ms",
                payload={
                    "model": self._settings.CLAUDE_CLI_MODEL,
                    "latency_ms": latency_ms,
                    "response": raw,
                    "stderr": stderr if stderr.strip() else None,
                    "session_id": session_id,
                },
                correlation_id=correlation_id,
            )
            raise LLMClientError(f"claude CLI reported error: {raw[:500]}")

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
            source="bob.llm_client.chat",
            summary=(
                f"LLM call terminé en {latency_ms:.0f}ms "
                f"({tokens_out if tokens_out is not None else '?'} tokens response)"
            ),
            payload={
                "response": raw,
                "latency_ms": latency_ms,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": self._settings.CLAUDE_CLI_MODEL,
                "session_id": session_id,
            },
            correlation_id=correlation_id,
        )

        return raw

    @staticmethod
    def _extract_result(
        stdout: str,
    ) -> tuple[str, int | None, int | None, bool]:
        """Pull ``result`` text + token counts out of ``claude -p --output-format json``.

        Returns ``(text, tokens_in, tokens_out, is_error)``. Falls back to the
        raw stdout if the wrapper JSON cannot be decoded — the response-parser
        layer will surface a friendly error then.
        """

        text = stdout.strip()
        if not text:
            return "", None, None, False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text, None, None, False
        if not isinstance(payload, dict):
            return text, None, None, False

        result = payload.get("result")
        if not isinstance(result, str):
            result = text
        result = _strip_code_fence(result)

        tokens_in: int | None = None
        tokens_out: int | None = None
        usage = payload.get("usage")
        if isinstance(usage, dict):
            for key in ("input_tokens", "prompt_tokens"):
                val = usage.get(key)
                if isinstance(val, int):
                    tokens_in = val
                    break
            for key in ("output_tokens", "completion_tokens"):
                val = usage.get(key)
                if isinstance(val, int):
                    tokens_out = val
                    break
        return result, tokens_in, tokens_out, bool(payload.get("is_error"))

    @staticmethod
    def _build_tools_system_addendum(tools: list[ToolDefinition]) -> str:
        """Build a system-prompt addendum describing the available tools.

        Asks Claude to emit a JSON object with a ``tool_calls`` array when it
        wants to invoke a tool, and plain text otherwise. The CLI doesn't
        expose Anthropic-format tool-calling on the command line for arbitrary
        tools, so we route through structured text instead.
        """

        tool_blocks: list[str] = []
        for tool in tools:
            schema = json.dumps(tool.parameters, ensure_ascii=False)
            tool_blocks.append(
                f"- name: {tool.name}\n"
                f"  description: {tool.description}\n"
                f"  parameters (JSON Schema): {schema}"
            )
        joined = "\n".join(tool_blocks)
        return (
            "\n\nYou have access to the following tools. To use one, respond "
            "with ONLY a JSON object on a single line, with no surrounding text "
            "and no markdown code fence:\n"
            '{"tool_calls": [{"id": "call_1", "name": "<name>", "arguments": {...}}]}\n'
            "You may include multiple entries in the ``tool_calls`` array to "
            "request several tool invocations at once. If you do NOT need a "
            "tool, respond with plain text (NO json wrapper).\n\n"
            f"Tools:\n{joined}"
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        if tools:
            addendum = self._build_tools_system_addendum(tools)
            augmented: list[dict[str, Any]] = []
            injected = False
            for msg in messages:
                if msg.get("role") == "system" and not injected:
                    augmented.append({**msg, "content": str(msg.get("content", "")) + addendum})
                    injected = True
                else:
                    augmented.append(msg)
            if not injected:
                augmented.insert(0, {"role": "system", "content": addendum.lstrip()})
        else:
            augmented = list(messages)

        raw = await self.chat(messages=augmented, session_id=session_id)

        if not tools:
            return LLMResponse(text=raw, tool_calls=[])

        stripped = _strip_code_fence(raw).strip()
        if not stripped.startswith("{"):
            return LLMResponse(text=raw, tool_calls=[])

        # Use ``raw_decode`` so a leading JSON object is recognised even when
        # Claude appends prose after it (observed in practice — the model
        # emits ``{"tool_calls": [...]}`` followed by a confirmation
        # sentence). Strict ``json.loads`` on the full string would fail and
        # the tool call would be silently lost.
        try:
            payload, _consumed = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError:
            return LLMResponse(text=raw, tool_calls=[])

        if not isinstance(payload, dict) or "tool_calls" not in payload:
            return LLMResponse(text=raw, tool_calls=[])

        raw_calls = payload.get("tool_calls")
        if not isinstance(raw_calls, list):
            raise LLMClientError(
                f"Claude CLI returned malformed tool call: 'tool_calls' is not a list "
                f"({type(raw_calls).__name__})"
            )

        tool_calls: list[ToolCall] = []
        for entry in raw_calls:
            if not isinstance(entry, dict):
                raise LLMClientError(
                    f"Claude CLI returned malformed tool call: entry is not an object "
                    f"({type(entry).__name__})"
                )
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise LLMClientError("Claude CLI returned malformed tool call: missing 'name'")
            arguments = entry.get("arguments", {})
            if not isinstance(arguments, dict):
                raise LLMClientError(
                    f"Claude CLI returned malformed tool call: 'arguments' is not an "
                    f"object ({type(arguments).__name__})"
                )
            raw_id = entry.get("id")
            call_id = raw_id if isinstance(raw_id, str) and raw_id else f"call_{uuid4().hex[:8]}"
            tool_calls.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=cast(dict[str, Any], arguments),
                )
            )

        if not tool_calls:
            return LLMResponse(text=raw, tool_calls=[])

        return LLMResponse(text=None, tool_calls=tool_calls)
