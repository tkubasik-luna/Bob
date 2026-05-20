"""LLM client abstraction and LM Studio implementation.

The abstract :class:`LLMClient` is intentionally tiny — a single ``chat``
method returning the raw string emitted by the model. Higher layers
(``response_parser``, ``chat_service``) take care of validation, retries
and structured-output parsing.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, cast

from openai import AsyncOpenAI

from bob.config import Settings
from bob.logging_setup import log_llm_call


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
        kwargs: dict[str, Any] = {
            "model": self._settings.LLM_MODEL,
            "messages": messages,
            "timeout": self._settings.LLM_TIMEOUT_SECONDS,
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": schema,
            }

        started = time.perf_counter()
        completion = await self._client.chat.completions.create(**kwargs)
        latency_ms = (time.perf_counter() - started) * 1000.0

        content = completion.choices[0].message.content
        raw = cast(str, content or "")

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

        return raw
