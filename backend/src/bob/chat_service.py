"""High-level chat orchestrator wiring Jarvis history + LLM + parser together.

The :class:`ChatService` is the single entry point used by the WebSocket
layer (and the smoke CLI) to turn a user message into a validated
:class:`ParsedResponse`. Collaborators are injected through ``__init__`` so
tests can swap in fakes without touching module globals.

Persistence: history lives in a singleton :class:`bob.jarvis_store.JarvisStore`
(SQLite-backed) rather than the legacy in-memory per-session store.
``session_id`` is forwarded to the LLM call log only.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Protocol

from bob import jarvis_store as jarvis_store_module
from bob import prompts as prompts_module
from bob import response_parser
from bob import ui_registry as ui_registry_module
from bob.config import get_settings
from bob.jarvis_store import JarvisStore
from bob.llm_client import ClaudeCliClient, LLMClient, LMStudioClient
from bob.ui_registry import ParsedResponse


class _PromptsLike(Protocol):
    def render(self, name: str, **kwargs: object) -> str: ...


class ChatService:
    """Orchestrate a single user → assistant turn end-to-end."""

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        jarvis_store: JarvisStore,
        jarvis_prompt: str,
        prompts: _PromptsLike = prompts_module,
        ui_registry: ModuleType = ui_registry_module,
    ) -> None:
        self._llm_client = llm_client
        self._jarvis_store = jarvis_store
        self._jarvis_prompt = jarvis_prompt
        self._prompts = prompts
        self._ui_registry = ui_registry

    async def handle_user_message(
        self,
        session_id: str,
        user_content: str,
    ) -> ParsedResponse:
        """Run one full turn (``session_id`` is informational only).

        Steps:
        1. Append the user message to the persistent Jarvis thread.
        2. Build ``[system_prompt, *history]`` for the LLM. The system prompt
           is the Jarvis personality from ``jarvis.md`` prepended to the
           existing ``system_chat`` template (which carries the UI components
           catalogue and JSON contract).
        3. Call the LLM with the response JSON schema.
        4. Validate / retry / fallback via :mod:`bob.response_parser`.
        5. Append the assistant ``speech`` to the persistent thread.
        """

        self._jarvis_store.append("user", user_content)

        ui_addendum = self._prompts.render(
            "system_chat",
            components_description=self._ui_registry.get_components_description_for_prompt(),
        )
        system_content = f"{self._jarvis_prompt}\n\n{ui_addendum}"

        history = self._jarvis_store.history()
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
        self._jarvis_store.append("assistant", parsed.speech)
        return parsed


def get_default_chat_service() -> ChatService:
    """Build a :class:`ChatService` wired with the runtime defaults.

    Relies on :func:`bob.jarvis_store.get_default_store` having been primed by
    the app lifespan (see :mod:`bob.main`). The Jarvis personality is read
    lazily via :func:`bob.jarvis_prompt_loader.load_jarvis_prompt` from the
    configured ``BOB_DATA_DIR``.
    """

    from bob.jarvis_prompt_loader import load_jarvis_prompt

    settings = get_settings()
    client: LLMClient
    if settings.LLM_PROVIDER == "claude_cli":
        client = ClaudeCliClient(settings)
    else:
        client = LMStudioClient(settings)

    return ChatService(
        llm_client=client,
        jarvis_store=jarvis_store_module.get_default_store(),
        jarvis_prompt=load_jarvis_prompt(settings.BOB_DATA_DIR),
    )
