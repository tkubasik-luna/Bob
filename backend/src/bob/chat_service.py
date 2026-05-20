"""High-level chat orchestrator wiring conversation + LLM + parser together.

The :class:`ChatService` is the single entry point used by the WebSocket
layer (and the smoke CLI) to turn a user message into a validated
:class:`ParsedResponse`. Collaborators are injected through ``__init__`` so
tests can swap in fakes without touching module globals.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Protocol

from bob import conversation as conversation_module
from bob import prompts as prompts_module
from bob import response_parser
from bob import ui_registry as ui_registry_module
from bob.config import get_settings
from bob.conversation import ConversationStore
from bob.llm_client import LLMClient, LMStudioClient
from bob.ui_registry import ParsedResponse


class _PromptsLike(Protocol):
    def render(self, name: str, **kwargs: object) -> str: ...


class ChatService:
    """Orchestrate a single user → assistant turn end-to-end."""

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        conversation: ConversationStore,
        prompts: _PromptsLike = prompts_module,
        ui_registry: ModuleType = ui_registry_module,
    ) -> None:
        self._llm_client = llm_client
        self._conversation = conversation
        self._prompts = prompts
        self._ui_registry = ui_registry

    async def handle_user_message(
        self,
        session_id: str,
        user_content: str,
    ) -> ParsedResponse:
        """Run one full turn for ``session_id``.

        Steps:
        1. Append the user message to the conversation.
        2. Build ``[system_prompt, *history]`` for the LLM.
        3. Call the LLM with the response JSON schema.
        4. Validate / retry / fallback via :mod:`bob.response_parser`.
        5. Append the assistant ``speech`` to the conversation.
        """

        self._conversation.append(session_id, "user", user_content)

        system_content = self._prompts.render(
            "system_chat",
            components_description=self._ui_registry.get_components_description_for_prompt(),
        )
        history = self._conversation.get_history(session_id)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            *({"role": m["role"], "content": m["content"]} for m in history),
        ]

        raw = await self._llm_client.chat(
            messages,
            schema=self._ui_registry.get_response_schema(),
            session_id=session_id,
        )
        parsed = await response_parser.parse(raw, self._llm_client, messages, session_id=session_id)
        self._conversation.append(session_id, "assistant", parsed.speech)
        return parsed


def get_default_chat_service() -> ChatService:
    """Build a :class:`ChatService` wired with the runtime defaults."""

    settings = get_settings()
    return ChatService(
        llm_client=LMStudioClient(settings),
        conversation=conversation_module.get_default_store(),
    )
