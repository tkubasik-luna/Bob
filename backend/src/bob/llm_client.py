"""LLM client abstraction and LM Studio implementation.

The abstract :class:`LLMClient` is intentionally tiny — a single ``chat``
method returning the raw string emitted by the model. Higher layers
(``response_parser``, ``chat_service``) take care of validation, retries
and structured-output parsing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, cast

from openai import AsyncOpenAI

from bob.config import Settings


class LLMClient(ABC):
    """Abstract interface for an OpenAI-compatible chat LLM."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
    ) -> str:
        """Send ``messages`` to the LLM and return the raw response string.

        If ``schema`` is provided, ask the backend for a JSON response matching
        the given JSON Schema (LM Studio's structured output feature).
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

        completion = await self._client.chat.completions.create(**kwargs)
        content = completion.choices[0].message.content
        return cast(str, content or "")
