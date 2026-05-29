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

Issue 0048 adds the ``system_validator`` role contract. Both clients
fold ``system_validator`` rows into ``system`` rows prefixed with
:data:`bob.validation.system_validator.FALLBACK_VALIDATOR_PREFIX` before
dispatching: LM Studio rejects unknown roles with HTTP 400
("'messages' array must only contain objects with a 'role' field that
is in [user, assistant, system, tool]"), and the Claude CLI's prompt
rendering only understands the four standard roles.
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
from bob.llm.tooling import (
    NativeToolCallParseError,
    ToolCodec,
    ToolSpec,
    capability_for_backend,
    order_specs,
    select_codec,
)
from bob.llm.types import LLMResponse, StreamChunk, ToolDefinition
from bob.logging_setup import log_llm_call
from bob.validation.system_validator import (
    FALLBACK_VALIDATOR_PREFIX,
    SYSTEM_VALIDATOR_ROLE,
)

_logger = structlog.get_logger(__name__)


#: Roles accepted by the OpenAI-compatible endpoints Bob targets
#: (LM Studio, vLLM, llama.cpp's server, Claude CLI in tool mode). LM
#: Studio enforces this set strictly — passing ``system_validator``
#: returns HTTP 400. The validation path therefore folds unknown roles
#: into ``system`` messages prefixed with :data:`FALLBACK_VALIDATOR_PREFIX`.
#: Issue 0048 — the fold path is documented in
#: :mod:`bob.validation.system_validator`.
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


def _assert_standard_roles(messages: list[dict[str, Any]]) -> None:
    """Belt-and-suspenders: raise if any non-standard role survived the fold.

    Issue 0048 post-mortem (logs 2026-05-28 11:46/11:48): a stale backend
    process running pre-fold code shipped ``system_validator`` rows straight
    to LM Studio and got a cryptic HTTP 400 in return. The fold is now
    wired into all three OpenAI-bound entry points (``chat`` / ``complete``
    / ``stream_complete``), but a future regression that drops the fold
    call would surface the same opaque 400 again. This helper turns that
    failure mode into a loud :class:`LLMClientError` raised before any
    network round-trip — caught by the unit tests below.

    Cost on the happy path is one frozenset membership per message; trivial.
    """

    for index, msg in enumerate(messages):
        role = msg.get("role")
        if role not in _STANDARD_ROLES:
            raise LLMClientError(
                f"Non-standard role {role!r} at messages[{index}] would be "
                f"rejected by the OpenAI-compatible endpoint. Expected one "
                f"of {sorted(_STANDARD_ROLES)}. Did you forget to call "
                f"_normalise_validator_role()?"
            )


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

    def supports_guided_json(self) -> bool:
        """Whether ``chat(schema=…)`` TOKEN-GATES the output to the schema.

        PRD 0008 / issue 0060. The sub-agent runner asks this to decide whether
        to emit its control envelope under ``response_format`` guided decoding
        (constrained output, valid by construction) or to fall back to the
        tolerant ``json.loads``-then-``parse_action`` path. The distinction is
        NOT "does ``chat`` accept a ``schema`` arg" — every client does — but
        "does passing it actually constrain the decode". :class:`LMStudioClient`
        sets ``response_format: {"type": "json_schema", …}`` (real grammar
        gating) and overrides this to return its declared
        :attr:`bob.llm.tooling.BackendCapability.guided_json`.
        :class:`ClaudeCliClient` only appends the schema to the prompt as prose
        (no gating), so it inherits the conservative ``False`` here and stays on
        the tolerant envelope-parse path (Hermes codec + later self-correction).
        """

        return False

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
        # PRD 0008 / issue 0058 — the codec owns the tool-calling wire format.
        # LM Studio declares native function calling; ``select_codec`` returns
        # the native codec under the default ``auto`` mode. Picked ONCE here so
        # there is no per-call format branching downstream.
        self._capability = capability_for_backend("lm_studio")
        self._tool_codec: ToolCodec = select_codec(
            self._capability,
            settings.LLM_TOOL_MODE,
        )

    def supports_guided_json(self) -> bool:
        """LM Studio gates ``chat(schema=…)`` via ``response_format`` (issue 0060).

        Reads the declared :class:`bob.llm.tooling.BackendCapability` (single
        source) rather than hard-coding ``True`` so a future capability change
        flows through. When ``True`` the sub-agent runner constrains its
        envelope under guided decoding; :meth:`chat` turns the ``schema`` into
        ``response_format: {"type": "json_schema", …}`` below.
        """

        return self._capability.guided_json

    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        # Issue 0048 — LM Studio rejects unknown roles with HTTP 400
        # ("'messages' array must only contain objects with a 'role'
        # field that is in [user, assistant, system, tool]"). Fold the
        # ``system_validator`` rows into prefixed ``system`` messages so
        # the validator payload still reads distinctly in the prompt.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=False)
        _assert_standard_roles(messages)
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
                summary=(f"LLM call returned empty content ({latency_ms:.0f}ms)"),
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
        # Issue 0048 — same fold policy as ``chat``: LM Studio rejects
        # unknown roles with HTTP 400. See :func:`_normalise_validator_role`.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=False)
        _assert_standard_roles(messages)
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": 4096,
        }
        # Issue 0058 — tool advertisement is delegated to the codec. For the
        # native codec this is the OpenAI ``tools`` + ``tool_choice`` block.
        if tools:
            specs = order_specs([ToolSpec.from_tool_definition(tool) for tool in tools])
            kwargs.update(self._tool_codec.inject(messages, specs))

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
        # Issue 0058 — parsing the native ``message.tool_calls`` surface is the
        # codec's job. A malformed-arguments raise is translated back into the
        # legacy ``LLMClientError`` (+ the same debug event) so the error
        # surface and the 0057 golden fixtures stay byte-identical.
        try:
            tool_calls = self._tool_codec.parse(message)
        except NativeToolCallParseError as exc:
            # The legacy path emitted the ``malformed tool args`` debug event
            # only for a JSON *decode* failure (not for the decoded-but-not-an-
            # object case). Preserve that split exactly.
            if exc.is_decode_error:
                emit_debug(
                    category="llm",
                    severity="error",
                    source="bob.llm_client.complete",
                    summary=f"LLM call malformed tool args ({latency_ms:.0f}ms)",
                    payload={
                        "model": self._settings.LLM_MODEL,
                        "latency_ms": latency_ms,
                        "arguments_raw": exc.arguments_raw,
                        "exception": str(exc),
                        "session_id": session_id,
                    },
                    correlation_id=correlation_id,
                )
            raise LLMClientError(exc.message) from exc

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

        # Issue 0048 — same fold policy as ``complete``.
        messages = _normalise_validator_role(messages, allow_arbitrary_roles=False)
        _assert_standard_roles(messages)
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
            "max_tokens": 4096,
            "stream": True,
        }
        # Issue 0058 — same codec injection as ``complete``.
        if tools:
            specs = order_specs([ToolSpec.from_tool_definition(tool) for tool in tools])
            kwargs.update(self._tool_codec.inject(messages, specs))

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

        # Issue 0058 — the codec's stream parser owns the per-``index``
        # tool-call accumulation and the ``tool_call_*`` chunk lifecycle. The
        # core keeps the text-mode passthrough, usage/token tracking and the
        # ``finally`` logging block (observability, not wire format). The
        # parser re-emits the same ``args_delta`` suffixes byte-for-byte so the
        # ``say`` tool's ``PartialJsonParser`` → ``speech_delta`` path is
        # unchanged.
        parser = self._tool_codec.stream_parser()
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

                # Tool-call deltas — delegated to the codec's stream parser.
                for chunk in parser.feed(delta):
                    yield chunk

                # Final usage chunk on some providers.
                usage = getattr(raw_chunk, "usage", None)
                if usage is not None:
                    tokens_in = getattr(usage, "prompt_tokens", None) or tokens_in
                    tokens_out = getattr(usage, "completion_tokens", None) or tokens_out

            # Stream exhausted — flush ``tool_call_end`` chunks. A malformed
            # final-args raise is translated back into the legacy
            # ``LLMClientError`` (+ the same debug event) before re-raising.
            try:
                for chunk in parser.finish():
                    yield chunk
            except NativeToolCallParseError as exc:
                if exc.is_decode_error:
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    emit_debug(
                        category="llm",
                        severity="error",
                        source="bob.llm_client.stream_complete",
                        summary=(f"LLM stream malformed final args ({latency_ms:.0f}ms)"),
                        payload={
                            "model": self._settings.LLM_MODEL,
                            "latency_ms": latency_ms,
                            "arguments_raw": exc.arguments_raw,
                            "exception": str(exc),
                            "session_id": session_id,
                        },
                        correlation_id=correlation_id,
                    )
                raise LLMClientError(exc.message) from exc
        finally:
            latency_ms = (time.perf_counter() - started) * 1000.0
            # Reconstruct a raw-for-log shape mirroring :meth:`complete`.
            log_calls = parser.log_calls
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
                    "tool_calls": log_calls,
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

    PRD 0008 / issue 0061 — tool calling goes through the
    :class:`bob.llm.tooling.hermes.HermesToolCodec`. The CLI has no native
    function calling and no constrained decoding, so the codec advertises the
    tools as a Nous-Hermes ``<tools>`` block in the system prompt and parses
    the model's ``<tool_call>`` replies through a tolerant chain
    (``json → ast.literal_eval → fenced-JSON``). The fragile hand-written
    ``{"tool_calls":[…]}`` addendum + brace-repair salvage that lived here are
    gone — only the wire format died; the Claude CLI stays first-class.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Issue 0061 — pick the codec ONCE (mirrors ``LMStudioClient``). The
        # ``claude_cli`` capability declares ``hermes_tags``; ``select_codec``
        # returns the Hermes codec under the default ``auto`` mode (or explicit
        # ``hermes``). No per-call format branching survives downstream.
        self._tool_codec: ToolCodec = select_codec(
            capability_for_backend("claude_cli"),
            settings.LLM_TOOL_MODE,
        )

    def _isolation_args(self) -> list[str]:
        """Extra argv that quarantine the CLI from the user's ``~/.claude``.

        ``--strict-mcp-config`` drops every inherited MCP server; an empty
        ``--setting-sources`` skips user/project/local settings so no
        SessionStart hook (e.g. a "caveman mode" plugin) injects a system
        prompt that competes with Bob's Jarvis persona. Keychain/OAuth auth
        survives — unlike ``--bare`` which would force ``ANTHROPIC_API_KEY``.
        Empty list when :attr:`Settings.CLAUDE_CLI_ISOLATED` is False.
        """

        if not self._settings.CLAUDE_CLI_ISOLATED:
            return []
        return ["--strict-mcp-config", "--setting-sources", ""]

    def _isolation_cwd(self) -> str | None:
        """Working directory for the subprocess, or ``None`` to inherit.

        Running from :attr:`Settings.BOB_DATA_DIR` (which has no ``CLAUDE.md``)
        stops the CLI auto-discovering the repo's ``CLAUDE.md`` and folding the
        project instructions into the prompt.
        """

        if not self._settings.CLAUDE_CLI_ISOLATED:
            return None
        return str(self._settings.BOB_DATA_DIR)

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
            *self._isolation_args(),
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
                cwd=self._isolation_cwd(),
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

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        session_id: str | None = None,
    ) -> LLMResponse:
        # Issue 0061 — tool advertisement is delegated to the codec. The Hermes
        # codec appends a ``<tools>`` block to the system message *in place*, so
        # we hand it a shallow copy of the message dicts to avoid mutating the
        # caller's list. ``inject`` returns ``{}`` for the CLI (the contract is
        # the prompt, not per-call kwargs) — we ignore the kwargs since
        # :meth:`chat` takes none.
        if tools:
            augmented: list[dict[str, Any]] = [dict(msg) for msg in messages]
            specs = order_specs([ToolSpec.from_tool_definition(tool) for tool in tools])
            self._tool_codec.inject(augmented, specs)
        else:
            augmented = list(messages)

        raw = await self.chat(messages=augmented, session_id=session_id)

        if not tools:
            return LLMResponse(text=raw, tool_calls=[])

        # Issue 0061 — parsing the ``<tool_call>`` reply is the codec's job. Its
        # tolerant chain (``json → ast.literal_eval → fenced-JSON``, no brace
        # counting) recovers the common garbled shapes; an unrecoverable reply
        # yields no calls and we fall back to plain text. Bounded-retry with
        # error echo for a still-malformed call is issue 0062, not here.
        tool_calls = self._tool_codec.parse(raw)
        if not tool_calls:
            return LLMResponse(text=raw, tool_calls=[])
        return LLMResponse(text=None, tool_calls=tool_calls)
