"""Jarvis orchestrator — user-turn entry point and sub-task dispatcher.

Replaces the previous ``chat_service`` module. ``Orchestrator.process_user_message``
is now the single user-message entry point used by the WebSocket layer and
the smoke CLI. The orchestrator:

1. Records the user message in the persistent Jarvis thread.
2. Asks Jarvis (LLM) whether to spawn a sub-task via the ``spawn_subtask``
   tool definition. If Jarvis emits a tool call:
   - Creates the task in :class:`TaskStore` (state ``pending``).
   - Transitions ``pending → running``.
   - Schedules a :class:`SubAgentRunner` as a background ``asyncio.Task``.
   - Returns a hard-coded confirmation as the user-visible reply.
3. Otherwise (no tool call → plain text reply), falls through to the
   structured-output path (JSON schema + ``response_parser``) so the
   server-driven UI contract from slice #0016 still applies.

Persistence: history lives in a singleton :class:`bob.jarvis_store.JarvisStore`
(SQLite-backed). ``session_id`` is forwarded to the LLM call log only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Protocol

import structlog

from bob import jarvis_store as jarvis_store_module
from bob import prompts as prompts_module
from bob import response_parser
from bob import task_store as task_store_module
from bob import ui_registry as ui_registry_module
from bob.config import get_settings
from bob.jarvis_store import JarvisStore
from bob.llm.types import ToolDefinition
from bob.llm_client import LLMClient
from bob.sub_agent_runner import SubAgentRunner
from bob.task_store import TaskStore, TaskStoreError
from bob.ui_registry import ComponentDescriptor

_logger = structlog.get_logger(__name__)


_SPAWN_CONFIRMATION = "D'accord, je m'en occupe. Je te dis dès que c'est prêt."


_TOOLS_SYSTEM_ADDENDUM = (
    "\n\nTu disposes d'un outil ``spawn_subtask`` pour déléguer une tâche "
    "longue ou autonome à un sub-agent en arrière-plan. Pour CE message, tu "
    "dois EXCLUSIVEMENT :\n"
    "- soit appeler ``spawn_subtask`` (et un seul appel) lorsque la demande "
    "mérite d'être déléguée ;\n"
    "- soit répondre directement en texte si une réponse immédiate suffit.\n"
    "Ne fais pas les deux."
)


_SPAWN_SUBTASK_TOOL = ToolDefinition(
    name="spawn_subtask",
    description=(
        "Délègue une tâche longue ou autonome à un sub-agent en arrière-plan. "
        "Utilise ceci quand l'utilisateur demande quelque chose qui prend du "
        "temps (recherche, draft d'email, analyse) ou qui peut tourner sans "
        "ton intervention. Pour les questions simples, réponds directement "
        "en texte sans appeler cet outil."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Titre court (1-5 mots) pour la sidebar.",
            },
            "goal": {
                "type": "string",
                "description": "Goal précis et complet pour le sub-agent.",
            },
        },
        "required": ["title", "goal"],
    },
)


class _PromptsLike(Protocol):
    def render(self, name: str, **kwargs: object) -> str: ...


@dataclass(frozen=True)
class OrchestratorResponse:
    """Outcome of a single user-message turn.

    - ``speech`` is the user-visible reply text (also routed through TTS).
    - ``ui`` carries the structured UI components from the no-spawn path.
      Empty when the turn resulted in one or more ``spawn_subtask`` calls.
    - ``spawned_task_ids`` lists every task created by this turn. Empty when
      no tool call was made.
    """

    speech: str
    ui: list[ComponentDescriptor] = field(default_factory=list)
    spawned_task_ids: list[str] = field(default_factory=list)


SubAgentRunnerFactory = Callable[[str], asyncio.Task[None]]


class Orchestrator:
    """Run one full user → assistant turn end-to-end, with optional spawn."""

    def __init__(
        self,
        *,
        jarvis_client: LLMClient,
        subagent_client: LLMClient,
        jarvis_store: JarvisStore,
        task_store: TaskStore,
        jarvis_prompt: str,
        sub_agent_runner_factory: SubAgentRunnerFactory | None = None,
        prompts: _PromptsLike = prompts_module,
        ui_registry: ModuleType = ui_registry_module,
    ) -> None:
        self._jarvis_client = jarvis_client
        self._subagent_client = subagent_client
        self._jarvis_store = jarvis_store
        self._task_store = task_store
        self._jarvis_prompt = jarvis_prompt
        self._prompts = prompts
        self._ui_registry = ui_registry
        self._sub_agent_runner_factory = sub_agent_runner_factory or self._default_runner_factory

    def _default_runner_factory(self, task_id: str) -> asyncio.Task[None]:
        runner = SubAgentRunner(
            subagent_client=self._subagent_client,
            task_store=self._task_store,
        )
        return asyncio.create_task(runner.run(task_id))

    async def process_user_message(
        self,
        session_id: str,
        user_content: str,
    ) -> OrchestratorResponse:
        """Run one full turn — may spawn 0..N sub-tasks before replying."""

        self._jarvis_store.append("user", user_content)

        system_content = self._build_system_prompt()
        history = self._jarvis_store.history()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content + _TOOLS_SYSTEM_ADDENDUM},
            *({"role": m["role"], "content": m["content"]} for m in history),
        ]

        decision = await self._jarvis_client.complete(
            messages,
            tools=[_SPAWN_SUBTASK_TOOL],
            session_id=session_id,
        )

        if decision.is_tool_call:
            spawned_task_ids = self._dispatch_spawns(decision.tool_calls)
            if spawned_task_ids:
                self._jarvis_store.append("assistant", _SPAWN_CONFIRMATION)
                return OrchestratorResponse(
                    speech=_SPAWN_CONFIRMATION,
                    ui=[],
                    spawned_task_ids=spawned_task_ids,
                )
            # All tool calls were rejected (bad args, unknown tool). Fall
            # through to the plain-text path so the user still gets a reply.
            _logger.warning(
                "orchestrator.tool_call_dropped_all",
                session_id=session_id,
                tool_call_count=len(decision.tool_calls),
            )

        return await self._reply_with_structured_response(
            session_id=session_id,
            base_messages=self._rebuild_chat_messages(system_content),
        )

    def _build_system_prompt(self) -> str:
        ui_addendum = self._prompts.render(
            "system_chat",
            components_description=self._ui_registry.get_components_description_for_prompt(),
        )
        return f"{self._jarvis_prompt}\n\n{ui_addendum}"

    def _rebuild_chat_messages(self, system_content: str) -> list[dict[str, Any]]:
        """Recompute the message list for the structured chat call.

        We do *not* reuse the message list passed to ``complete()`` because it
        included the tools-system addendum. The structured-output path takes
        a fresh system prompt + the persisted history (which now includes the
        user turn appended at the top of :meth:`process_user_message`).
        """

        history = self._jarvis_store.history()
        return [
            {"role": "system", "content": system_content},
            *({"role": m["role"], "content": m["content"]} for m in history),
        ]

    def _dispatch_spawns(self, tool_calls: list[Any]) -> list[str]:
        """Persist every valid ``spawn_subtask`` call and schedule its runner.

        Invalid calls (wrong name, missing args, bad arg types) are skipped
        with a warning so the orchestrator can still proceed.
        """

        spawned: list[str] = []
        for call in tool_calls:
            if call.name != _SPAWN_SUBTASK_TOOL.name:
                _logger.warning(
                    "orchestrator.unknown_tool",
                    tool_name=call.name,
                )
                continue
            title = call.arguments.get("title")
            goal = call.arguments.get("goal")
            if not isinstance(title, str) or not title.strip():
                _logger.warning("orchestrator.spawn_bad_title", arguments=call.arguments)
                continue
            if not isinstance(goal, str) or not goal.strip():
                _logger.warning("orchestrator.spawn_bad_goal", arguments=call.arguments)
                continue

            task_id = self._task_store.create_task(title=title, goal=goal)
            try:
                self._task_store.update_state(task_id, "running")
            except TaskStoreError:
                _logger.exception(
                    "orchestrator.spawn_transition_failed",
                    task_id=task_id,
                )
                continue
            self._sub_agent_runner_factory(task_id)
            _logger.info("orchestrator.spawned_subtask", task_id=task_id, title=title)
            spawned.append(task_id)
        return spawned

    async def _reply_with_structured_response(
        self,
        *,
        session_id: str,
        base_messages: list[dict[str, Any]],
    ) -> OrchestratorResponse:
        """Run the structured (JSON schema + retry/fallback) reply path."""

        raw = await self._jarvis_client.chat(
            base_messages,
            schema=self._ui_registry.get_response_schema(),
            session_id=session_id,
        )
        parsed = await response_parser.parse(
            raw,
            self._jarvis_client,
            base_messages,
            session_id=session_id,
        )
        self._jarvis_store.append("assistant", parsed.speech)
        return OrchestratorResponse(
            speech=parsed.speech,
            ui=list(parsed.ui),
            spawned_task_ids=[],
        )


def get_default_orchestrator() -> Orchestrator:
    """Build an :class:`Orchestrator` wired with runtime defaults.

    Relies on the singletons primed by :func:`bob.main.lifespan`. Tests that
    bypass the lifespan should use the DI constructor directly.
    """

    from bob.jarvis_prompt_loader import load_jarvis_prompt
    from bob.llm.factory import build_jarvis_client, build_subagent_client

    settings = get_settings()
    return Orchestrator(
        jarvis_client=build_jarvis_client(settings),
        subagent_client=build_subagent_client(settings),
        jarvis_store=jarvis_store_module.get_default_store(),
        task_store=task_store_module.get_default_store(),
        jarvis_prompt=load_jarvis_prompt(settings.BOB_DATA_DIR),
    )
