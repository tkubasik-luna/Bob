"""Jarvis orchestrator — user-turn entry point and sub-task dispatcher.

``Orchestrator.process_user_message`` is the single user-message entry point
used by the WebSocket layer and the smoke CLI. The orchestrator:

1. Records the user message in the persistent Jarvis thread.
2. Asks Jarvis (LLM) whether to spawn a sub-task or forward an answer to a
   sub-task waiting for input, via the ``spawn_subtask`` /
   ``forward_to_subtask`` tool definitions.
3. On ``spawn_subtask`` it creates the task in :class:`TaskStore`, emits
   ``task_created`` then hands it to :class:`TaskScheduler` which decides
   whether to promote it to ``running`` immediately or queue it.
4. On ``forward_to_subtask`` it appends the user's answer as a ``user``
   message on the target task, then asks the scheduler to resume the
   sub-agent. The orchestrator emits a short confirmation back to the user.
5. Otherwise (no tool call → plain text reply), it falls through to the
   structured-output path (JSON schema + ``response_parser``) so the
   server-driven UI contract from slice #0016 still applies.

Slice #0021 added :meth:`generate_proactive_message`: when a sub-agent
emits ``ask_user`` the :class:`ProactivityHandler` invokes this method.
Jarvis paraphrases the raw question in his own tone and the orchestrator
pushes a single ``assistant_msg`` with ``proactive: true`` back through the
WS emitter — no user turn triggered it.

Slice #0025 extends proactivity to ``done`` events (sub-task synthesis) and
introduces a per-instance buffering layer so proactive pushes do not race
with the user. Two flags gate the flush:

- ``_jarvis_state`` — set to ``thinking`` while a user turn is running, back
  to ``idle`` on exit. Buffer holds while non-idle so a paraphrased question
  doesn't pop in front of the user's own reply.
- ``_user_typing`` — flipped to ``true`` by ``client_typing`` WS heartbeats
  and back to ``false`` either on the next ``client_typing=false`` or
  automatically after 2 s of inactivity (server-side debounce).

Events arriving while either flag is active queue on
``_proactive_queue`` and flush FIFO once both clear. The background flusher
task is started by :meth:`start_proactive_loop` (called from the FastAPI
lifespan) and cancelled by :meth:`stop_proactive_loop`.

Persistence: Jarvis history lives in a singleton
:class:`bob.jarvis_store.JarvisStore` (SQLite-backed). ``session_id`` is
forwarded to the LLM call log only.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Literal, Protocol

import structlog

from bob import jarvis_store as jarvis_store_module
from bob import prompts as prompts_module
from bob import response_parser, task_scheduler, ws_events
from bob import task_store as task_store_module
from bob import ui_registry as ui_registry_module
from bob.config import get_settings
from bob.context.assembler import ContextAssembler
from bob.context.policy import ContextPolicy, legacy_full_history_policy
from bob.context.providers.legacy_full_history import LegacyFullHistoryProvider
from bob.debug_log import emit_debug, start_turn
from bob.jarvis_store import JarvisStore
from bob.llm.types import ToolDefinition
from bob.llm_client import LLMClient
from bob.task_store import TaskStore, TaskStoreError
from bob.ui_registry import ComponentDescriptor

_logger = structlog.get_logger(__name__)


_SPAWN_CONFIRMATION = "D'accord, je m'en occupe. Je te dis dès que c'est prêt."
_FORWARD_CONFIRMATION = "Compris, je transmets à la tâche."
_CANCEL_CONFIRMATION = "Compris, j'annule."


_TOOLS_SYSTEM_ADDENDUM = (
    "\n\nTu disposes de trois outils :\n"
    "- ``spawn_subtask`` : pour déléguer une tâche longue ou autonome à un "
    "sub-agent en arrière-plan.\n"
    "- ``forward_to_subtask`` : pour transmettre la réponse de l'utilisateur "
    "à une sous-tâche en attente d'input. Tu connais l'``id`` de chaque "
    "sous-tâche concernée via le résumé des tâches actives ci-dessous.\n"
    "- ``cancel_subtask`` : pour annuler une sous-tâche en cours quand "
    "l'utilisateur demande explicitement de l'arrêter (\"annule X\", "
    '"laisse tomber").\n'
    "Pour CE message, tu dois EXCLUSIVEMENT :\n"
    "- soit appeler ``spawn_subtask`` (un seul appel) si la demande mérite "
    "d'être déléguée ;\n"
    "- soit appeler ``forward_to_subtask`` si l'utilisateur répond à une "
    "question préalablement transmise par toi pour le compte d'une tâche en "
    "cours ;\n"
    "- soit appeler ``cancel_subtask`` si l'utilisateur demande explicitement "
    "d'annuler / arrêter une tâche listée dans le résumé ;\n"
    "- soit répondre directement en texte si aucune action n'est requise.\n"
    "Ne fais jamais deux appels en parallèle."
)


# Hard-coded template used by ``generate_proactive_message`` to paraphrase a
# sub-agent's ``ask_user`` question in Jarvis' tone. Slice #0021 pins this
# in code (no `jarvis.md`-driven tuning) so it ships deterministically.
_ASK_USER_PARAPHRASE_TEMPLATE = (
    "Une de tes sous-tâches ({task_title}) a besoin d'une info : "
    "'{raw_question}'. Reformule cette question pour l'utilisateur dans "
    "ton ton, en 1-2 phrases max. Ne mentionne pas le mot 'sub-agent', "
    "dis 'la tâche'."
)


# Slice #0025: hard-coded template used by ``generate_done_synthesis`` to
# announce a sub-task's completion in Jarvis' tone. Same pinning rationale as
# the ask_user paraphrase — no jarvis.md tuning, ships deterministically.
_DONE_SYNTHESIS_TEMPLATE = (
    "La sous-tâche '{task_title}' vient de terminer.\n"
    "Résultat brut : '{result}'.\n"
    "Étape 1 — Vérifie le contenu : si le résultat est vide, incohérent ou "
    "manifestement raté, dis-le franchement à l'utilisateur en une phrase "
    "et arrête-toi là.\n"
    "Étape 2 — Sinon, ouvre impérativement par "
    "« Voilà ce que j'ai trouvé à propos de <sujet> … » (remplace <sujet> "
    "par le thème exact de la sous-tâche, pas son titre brut) puis résume "
    "les points clés en 2-3 lignes max dans ton ton. "
    "Propose une suite si pertinent."
)


# Debounce window before ``_user_typing`` falls back to false on its own. The
# frontend already debounces keystrokes at 500ms, but a network hiccup could
# drop the trailing ``client_typing=false`` — we don't want a proactive push
# to stay queued forever.
_USER_TYPING_GRACE_S = 2.0

# Polling cadence used by the flusher while waiting for ``_jarvis_state`` and
# ``_user_typing`` to clear. Small enough to feel instant; the loop only ever
# spins after pulling an event from the queue.
_FLUSH_POLL_INTERVAL_S = 0.05


# Event kinds accepted by the proactive queue. The literal is exposed so
# call sites (handler + tests) cannot smuggle in a typo.
ProactiveEventKind = Literal["ask_user", "done"]


JarvisState = Literal["idle", "thinking"]


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


_FORWARD_TO_SUBTASK_TOOL = ToolDefinition(
    name="forward_to_subtask",
    description=(
        "Transmet la réponse de l'utilisateur à une sous-tâche en attente "
        "d'input. À appeler uniquement quand l'utilisateur répond à une "
        "question préalablement transmise par toi pour le compte d'une "
        "tâche en cours."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "ID de la sous-tâche concernée. Le résumé des tâches "
                    "actives en tête de prompt liste l'``id`` exact de chaque "
                    "tâche qui attend une réponse."
                ),
            },
            "response": {
                "type": "string",
                "description": ("La réponse de l'utilisateur à transmettre, telle quelle."),
            },
        },
        "required": ["task_id", "response"],
    },
)


_CANCEL_SUBTASK_TOOL = ToolDefinition(
    name="cancel_subtask",
    description=(
        "Annule une sous-tâche en cours. À appeler quand l'utilisateur "
        'demande explicitement d\'arrêter une tâche ("annule X", "laisse '
        'tomber"). Tu peux fournir une raison concise (sinon "user_cancelled" '
        "est utilisé)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "ID de la sous-tâche à annuler. Le résumé des tâches "
                    "actives en tête de prompt liste l'``id`` exact."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Raison brève. Default 'user_cancelled'.",
            },
        },
        "required": ["task_id"],
    },
)


class _PromptsLike(Protocol):
    def render(self, name: str, **kwargs: object) -> str: ...


class _SchedulerLike(Protocol):
    async def enqueue(self, task_id: str) -> None: ...

    async def resume(self, task_id: str) -> None: ...

    async def cancel(self, task_id: str, *, reason: str = ...) -> None: ...


@dataclass(frozen=True)
class OrchestratorResponse:
    """Outcome of a single user-message turn.

    - ``speech`` is the user-visible reply text (also routed through TTS).
    - ``ui`` carries the structured UI components from the no-spawn path.
      Empty when the turn resulted in one or more ``spawn_subtask`` /
      ``forward_to_subtask`` / ``cancel_subtask`` calls.
    - ``spawned_task_ids`` lists every task created by this turn. Empty
      when no spawn happened (forwards or plain-text replies).
    - ``forwarded_task_ids`` lists every task the user's message was
      forwarded to. Empty when the turn didn't include a forward.
    - ``cancelled_task_ids`` lists every task cancelled by this turn via
      the ``cancel_subtask`` tool. Empty when the turn didn't include a
      cancellation.
    """

    speech: str
    ui: list[ComponentDescriptor] = field(default_factory=list)
    spawned_task_ids: list[str] = field(default_factory=list)
    forwarded_task_ids: list[str] = field(default_factory=list)
    cancelled_task_ids: list[str] = field(default_factory=list)


class Orchestrator:
    """Run one full user → assistant turn end-to-end, with optional spawn / forward."""

    def __init__(
        self,
        *,
        jarvis_client: LLMClient,
        jarvis_store: JarvisStore,
        task_store: TaskStore,
        task_scheduler: _SchedulerLike,
        jarvis_prompt: str,
        prompts: _PromptsLike = prompts_module,
        ui_registry: ModuleType = ui_registry_module,
        context_policy: ContextPolicy | None = None,
    ) -> None:
        self._jarvis_client = jarvis_client
        self._jarvis_store = jarvis_store
        self._task_store = task_store
        self._task_scheduler = task_scheduler
        self._jarvis_prompt = jarvis_prompt
        self._prompts = prompts
        self._ui_registry = ui_registry
        # PRD 0006 / issue 0043 — every prompt the orchestrator sends is now
        # composed by ``ContextAssembler``. The default policy
        # ``legacy_full_history`` reproduces today's "send the whole thread
        # every turn" behavior; later slices swap the provider mix without
        # touching the orchestrator.
        self._context_policy = context_policy or legacy_full_history_policy()

        # Slice #0025 — proactive buffering layer. The queue + flusher live
        # on the instance so tests can build an Orchestrator without auto-
        # starting the loop (and drain manually). Production wiring calls
        # ``start_proactive_loop()`` from the FastAPI lifespan.
        self._jarvis_state: JarvisState = "idle"
        self._user_typing: bool = False
        self._proactive_queue: asyncio.Queue[tuple[str, ProactiveEventKind]] = asyncio.Queue()
        self._flusher_task: asyncio.Task[None] | None = None
        self._typing_reset_task: asyncio.Task[None] | None = None

    async def process_user_message(
        self,
        session_id: str,
        user_content: str,
    ) -> OrchestratorResponse:
        """Run one full turn — may spawn 0..N sub-tasks or forward to one before replying."""

        # Debug view (PRD 0005, slice 0039): generate a fresh turn_id and
        # install it in the ContextVar BEFORE the first ``emit_debug`` call so
        # every subsequent event in this turn — including those emitted by
        # sub-tasks spawned via ``asyncio.create_task`` from within the turn —
        # inherits the same id automatically through ``contextvars``.
        start_turn()

        emit_debug(
            category="input",
            severity="info",
            source="orchestrator.process_user_message",
            summary=f'User envoie: "{user_content[:80]}"',
            payload={"content": user_content, "session_id": session_id},
        )

        self._jarvis_state = "thinking"
        try:
            self._jarvis_store.append("user", user_content)

            emit_debug(
                category="decision",
                severity="info",
                source="orchestrator.process_user_message",
                summary="Jarvis réfléchit",
                payload={"session_id": session_id},
            )

            system_content = self._build_system_prompt()
            waiting_context = self._build_waiting_input_addendum()
            complete_system = system_content + _TOOLS_SYSTEM_ADDENDUM + waiting_context
            messages = self._assemble_chat_messages(
                system_content=complete_system,
                user_message=user_content,
            )

            decision = await self._jarvis_client.complete(
                messages,
                tools=[_SPAWN_SUBTASK_TOOL, _FORWARD_TO_SUBTASK_TOOL, _CANCEL_SUBTASK_TOOL],
                session_id=session_id,
            )

            emit_debug(
                category="decision",
                severity="info",
                source="orchestrator.process_user_message",
                summary="Jarvis a fini de réfléchir",
                payload={
                    "session_id": session_id,
                    "is_tool_call": decision.is_tool_call,
                    "tool_call_count": len(decision.tool_calls),
                },
            )

            if decision.is_tool_call:
                (
                    spawned_task_ids,
                    forwarded_task_ids,
                    cancelled_task_ids,
                ) = await self._dispatch_tool_calls(decision.tool_calls)
                if spawned_task_ids or forwarded_task_ids or cancelled_task_ids:
                    # Pick the speech to match the dominant action. We instruct
                    # Jarvis to pick one path so coexistence is rare; if multiple
                    # happen we prioritise the most user-visible signal: cancel
                    # → forward → spawn.
                    if cancelled_task_ids:
                        speech = _CANCEL_CONFIRMATION
                    elif forwarded_task_ids:
                        speech = _FORWARD_CONFIRMATION
                    else:
                        speech = _SPAWN_CONFIRMATION
                    self._jarvis_store.append("assistant", speech)
                    emit_debug(
                        category="output",
                        severity="info",
                        source="orchestrator.process_user_message",
                        summary=f'Bob répond: "{speech[:80]}"',
                        payload={
                            "speech": speech,
                            "ui": [],
                            "proactive": False,
                            "spawned_task_ids": spawned_task_ids,
                            "forwarded_task_ids": forwarded_task_ids,
                            "cancelled_task_ids": cancelled_task_ids,
                        },
                    )
                    return OrchestratorResponse(
                        speech=speech,
                        ui=[],
                        spawned_task_ids=spawned_task_ids,
                        forwarded_task_ids=forwarded_task_ids,
                        cancelled_task_ids=cancelled_task_ids,
                    )
                # All tool calls were rejected (bad args, unknown tool). Fall
                # through to the plain-text path so the user still gets a reply.
                _logger.warning(
                    "orchestrator.tool_call_dropped_all",
                    session_id=session_id,
                    tool_call_count=len(decision.tool_calls),
                )

            response = await self._reply_with_structured_response(
                session_id=session_id,
                base_messages=self._rebuild_chat_messages(system_content),
            )
            emit_debug(
                category="output",
                severity="info",
                source="orchestrator.process_user_message",
                summary=f'Bob répond: "{response.speech[:80]}"',
                payload={
                    "speech": response.speech,
                    "ui": [component.model_dump() for component in response.ui],
                    "proactive": False,
                },
            )
            return response
        finally:
            self._jarvis_state = "idle"

    async def generate_proactive_message(self, task_id: str, event_kind: str) -> None:
        """Enqueue a proactive Jarvis push for ``task_id``.

        Slice #0021 handled ``ask_user``. Slice #0025 adds ``done`` and routes
        both kinds through ``_proactive_queue`` so the flusher can defer
        emission while the user is mid-turn (``_jarvis_state="thinking"``) or
        typing (``_user_typing=True``).

        Errors at render time (unknown task, missing question, LLM failure,
        empty output) are logged and swallowed inside the renderers —
        proactivity must never crash the producer subscriber.
        """

        if event_kind not in ("ask_user", "done"):
            _logger.info(
                "orchestrator.proactive_event_ignored",
                task_id=task_id,
                event_kind=event_kind,
            )
            return

        kind: ProactiveEventKind = event_kind  # type: ignore[assignment]
        await self._proactive_queue.put((task_id, kind))
        _logger.debug(
            "orchestrator.proactive_enqueued",
            task_id=task_id,
            event_kind=event_kind,
            queue_size=self._proactive_queue.qsize(),
        )

    async def generate_done_synthesis(self, task_id: str) -> None:
        """Public wrapper for slice #0025: enqueue a ``done`` synthesis.

        Kept distinct from :meth:`generate_proactive_message` so call sites
        (and tests) can be explicit about what they want. Under the hood it
        just delegates to the unified queue path.
        """

        await self.generate_proactive_message(task_id, "done")

    # --- Proactive flusher ---------------------------------------------------

    def start_proactive_loop(self) -> None:
        """Start the background flusher that drains ``_proactive_queue``.

        Idempotent: a second call is a no-op while the previous task is
        still alive. Called from the FastAPI lifespan once the orchestrator
        singleton is wired; tests can call it explicitly when they need the
        loop, otherwise they invoke ``_do_*`` directly.
        """

        if self._flusher_task is not None and not self._flusher_task.done():
            return
        self._flusher_task = asyncio.create_task(self._flush_proactive_loop())

    async def stop_proactive_loop(self) -> None:
        """Cancel the background flusher (and any pending typing reset)."""

        task = self._flusher_task
        self._flusher_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        reset = self._typing_reset_task
        self._typing_reset_task = None
        if reset is not None and not reset.done():
            reset.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reset

    def set_user_typing(self, value: bool) -> None:
        """Update the typing flag (with a 2s grace timer on the True path).

        Called from the WS layer when a ``client_typing`` event arrives.
        Setting True schedules an auto-reset to False after
        :data:`_USER_TYPING_GRACE_S` so a missing trailing False (network
        hiccup, browser tab killed mid-typing) cannot starve the queue.
        Each True restarts the timer.
        """

        self._user_typing = value
        # Cancel any pending reset; we'll reschedule below if value is True.
        reset = self._typing_reset_task
        self._typing_reset_task = None
        if reset is not None and not reset.done():
            reset.cancel()

        if value:
            try:
                self._typing_reset_task = asyncio.create_task(self._auto_reset_typing())
            except RuntimeError:
                # No running loop (sync call sites in narrow tests). The
                # flag is still honoured for the next flusher pass; manual
                # reset via ``set_user_typing(False)`` is always available.
                self._typing_reset_task = None

    async def _auto_reset_typing(self) -> None:
        """Reset ``_user_typing`` to False after the grace window."""

        try:
            await asyncio.sleep(_USER_TYPING_GRACE_S)
        except asyncio.CancelledError:
            return
        self._user_typing = False
        self._typing_reset_task = None

    async def _flush_proactive_loop(self) -> None:
        """Drain ``_proactive_queue`` FIFO, gating each event on user idleness."""

        while True:
            try:
                task_id, kind = await self._proactive_queue.get()
            except asyncio.CancelledError:
                return
            try:
                # Park until Jarvis is idle AND the user has stopped typing.
                # Polling is cheap (50ms) and only happens while there's an
                # event to flush; the rest of the time the loop is parked on
                # ``queue.get()``.
                while self._jarvis_state != "idle" or self._user_typing:
                    await asyncio.sleep(_FLUSH_POLL_INTERVAL_S)

                if kind == "ask_user":
                    await self._do_generate_ask_user_paraphrase(task_id)
                else:
                    await self._do_generate_done_synthesis(task_id)
            except asyncio.CancelledError:
                return
            except Exception:
                _logger.exception(
                    "orchestrator.proactive_flush_failed",
                    task_id=task_id,
                    event_kind=kind,
                )

    async def _do_generate_ask_user_paraphrase(self, task_id: str) -> None:
        """Synthesise + push the paraphrased ``ask_user`` question for ``task_id``."""

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.warning(
                "orchestrator.proactive_task_missing",
                task_id=task_id,
            )
            return

        question = self._latest_ask_user_question(task_id)
        if question is None:
            _logger.warning(
                "orchestrator.proactive_no_question",
                task_id=task_id,
            )
            return

        prompt = _ASK_USER_PARAPHRASE_TEMPLATE.format(
            task_title=task.title,
            raw_question=question,
        )
        text = await self._render_proactive_text(task_id, prompt)
        if text is None:
            return
        await self._push_proactive_assistant_msg(text)

    async def _do_generate_done_synthesis(self, task_id: str) -> None:
        """Synthesise + push the ``done`` announcement for ``task_id``."""

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.warning(
                "orchestrator.proactive_task_missing",
                task_id=task_id,
            )
            return

        result_text = task.result if task.result is not None else ""
        prompt = _DONE_SYNTHESIS_TEMPLATE.format(
            task_title=task.title,
            result=result_text,
        )
        text = await self._render_proactive_text(task_id, prompt)
        if text is None:
            return
        await self._push_proactive_assistant_msg(text)

    async def _render_proactive_text(self, task_id: str, prompt: str) -> str | None:
        """Run a single-turn chat() and return the trimmed text (or None)."""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._jarvis_prompt},
            {"role": "user", "content": prompt},
        ]
        try:
            raw = await self._jarvis_client.chat(messages, session_id=task_id)
        except Exception:
            _logger.exception(
                "orchestrator.proactive_llm_failed",
                task_id=task_id,
            )
            return None
        text = raw.strip()
        if not text:
            _logger.warning(
                "orchestrator.proactive_empty_text",
                task_id=task_id,
            )
            return None
        return text

    async def _push_proactive_assistant_msg(self, text: str) -> None:
        """Persist ``text`` in the Jarvis thread and emit the WS event."""

        # Persist in the singleton thread so the next user turn sees it as
        # part of the conversation history.
        self._jarvis_store.append("assistant", text)

        emit_debug(
            category="output",
            severity="info",
            source="orchestrator._push_proactive_assistant_msg",
            summary=f'Bob répond: "{text[:80]}"',
            payload={
                "speech": text,
                "ui": [],
                "proactive": True,
            },
        )

        await ws_events.emit(
            {
                "type": "assistant_msg",
                "msg_id": uuid.uuid4().hex,
                "speech": text,
                "ui": [],
                "proactive": True,
            }
        )

    def _build_system_prompt(self) -> str:
        ui_addendum = self._prompts.render(
            "system_chat",
            components_description=self._ui_registry.get_components_description_for_prompt(),
        )
        return f"{self._jarvis_prompt}\n\n{ui_addendum}"

    def _build_waiting_input_addendum(self) -> str:
        """Render a system-prompt suffix listing tasks waiting for the user.

        Empty string when no task is in ``waiting_input``. Each line carries
        ``task_id``, ``title`` and the most recent ``ask_user`` question so
        Jarvis can pick the right id for ``forward_to_subtask``.
        """

        try:
            waiting = self._task_store.list_tasks(state="waiting_input")
        except TaskStoreError:
            _logger.exception("orchestrator.waiting_list_failed")
            return ""
        if not waiting:
            return ""

        lines = ["\n\nSous-tâches en attente de réponse de l'utilisateur :"]
        for task in waiting:
            question = self._latest_ask_user_question(task.id) or "<question inconnue>"
            lines.append(f'  - task_id="{task.id}" — title="{task.title}" — question="{question}"')
        return "\n".join(lines)

    def _latest_ask_user_question(self, task_id: str) -> str | None:
        """Return the most recent ``ask_user`` question for ``task_id`` (or None)."""

        try:
            messages = self._task_store.get_task_messages(task_id)
        except TaskStoreError:
            return None
        for msg in reversed(messages):
            if msg.action == "ask_user":
                return msg.content
        return None

    def _rebuild_chat_messages(self, system_content: str) -> list[dict[str, Any]]:
        """Recompute the message list for the structured chat call.

        We do *not* reuse the message list passed to ``complete()`` because it
        included the tools-system addendum. The structured-output path takes
        a fresh system prompt + the persisted history (which now includes the
        user turn appended at the top of :meth:`process_user_message`).

        The actual composition is delegated to
        :class:`bob.context.assembler.ContextAssembler` (issue 0043). The
        orchestrator no longer reads ``jarvis_store`` directly — the
        ``LegacyFullHistoryProvider`` does that on its behalf.
        """

        return self._assemble_chat_messages(system_content=system_content)

    def _assemble_chat_messages(
        self,
        *,
        system_content: str,
        user_message: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build a chat-messages list via :class:`ContextAssembler`.

        ``system_content`` is the system prompt for this LLM call (may include
        tools / waiting addendums for the ``complete()`` path or be the bare
        Jarvis system prompt for the structured ``chat()`` path). The
        assembler concatenates the system entry with the persisted Jarvis
        thread emitted by :class:`LegacyFullHistoryProvider` — preserving
        today's byte-equal output.
        """

        provider = LegacyFullHistoryProvider(
            jarvis_store=self._jarvis_store,
            system_content=system_content,
        )
        assembler = ContextAssembler(providers=[provider], policy=self._context_policy)
        return assembler.assemble(user_message=user_message)

    async def _dispatch_tool_calls(
        self, tool_calls: list[Any]
    ) -> tuple[list[str], list[str], list[str]]:
        """Dispatch every valid spawn / forward / cancel call.

        Returns ``(spawned, forwarded, cancelled)``. Invalid calls (wrong
        name, missing args, bad arg types) are skipped with a warning so the
        orchestrator can still proceed.
        """

        spawned: list[str] = []
        forwarded: list[str] = []
        cancelled: list[str] = []
        for call in tool_calls:
            if call.name == _SPAWN_SUBTASK_TOOL.name:
                task_id = await self._dispatch_spawn(call)
                if task_id is not None:
                    spawned.append(task_id)
            elif call.name == _FORWARD_TO_SUBTASK_TOOL.name:
                task_id = await self._dispatch_forward(call)
                if task_id is not None:
                    forwarded.append(task_id)
            elif call.name == _CANCEL_SUBTASK_TOOL.name:
                task_id = await self._dispatch_cancel(call)
                if task_id is not None:
                    cancelled.append(task_id)
            else:
                _logger.warning(
                    "orchestrator.unknown_tool",
                    tool_name=call.name,
                )
        return spawned, forwarded, cancelled

    async def _dispatch_spawn(self, call: Any) -> str | None:
        """Persist a single ``spawn_subtask`` call and hand it to the scheduler.

        Returns the new task id on success, ``None`` when the call is
        malformed and was dropped. Emits ``task_created`` (state=pending)
        immediately after creation. The scheduler is responsible for the
        ``pending → running`` transition + the matching ``task_updated``
        event, since promotion can be deferred when the running cap is
        saturated.
        """

        title = call.arguments.get("title")
        goal = call.arguments.get("goal")
        if not isinstance(title, str) or not title.strip():
            _logger.warning("orchestrator.spawn_bad_title", arguments=call.arguments)
            return None
        if not isinstance(goal, str) or not goal.strip():
            _logger.warning("orchestrator.spawn_bad_goal", arguments=call.arguments)
            return None

        task_id = self._task_store.create_task(title=title, goal=goal)
        created = self._task_store.get_task(task_id)
        emit_debug(
            category="decision",
            severity="info",
            source="orchestrator._dispatch_spawn",
            summary=f"Jarvis lance sub-task '{title}'",
            payload={
                "task_id": task_id,
                "title": created.title,
                "goal": created.goal,
            },
        )
        await ws_events.emit(
            {
                "type": "task_created",
                "task_id": task_id,
                "title": created.title,
                "goal": created.goal,
                "state": created.state,
                "created_at": created.created_at,
            }
        )
        await self._task_scheduler.enqueue(task_id)
        _logger.info("orchestrator.spawned_subtask", task_id=task_id, title=title)
        return task_id

    async def _dispatch_forward(self, call: Any) -> str | None:
        """Forward the user's answer to a sub-agent in ``waiting_input``.

        Appends a ``user`` message to the task's log and asks the scheduler to
        resume the runner. Drops the call (with a warning) when the target id
        is unknown or the task is not in ``waiting_input``.
        """

        target_id = call.arguments.get("task_id")
        response_text = call.arguments.get("response")
        if not isinstance(target_id, str) or not target_id.strip():
            _logger.warning("orchestrator.forward_bad_task_id", arguments=call.arguments)
            return None
        if not isinstance(response_text, str) or not response_text.strip():
            _logger.warning("orchestrator.forward_bad_response", arguments=call.arguments)
            return None

        try:
            task = self._task_store.get_task(target_id)
        except TaskStoreError:
            _logger.warning("orchestrator.forward_unknown_task", task_id=target_id)
            return None

        if task.state != "waiting_input":
            _logger.warning(
                "orchestrator.forward_wrong_state",
                task_id=target_id,
                state=task.state,
            )
            return None

        try:
            message_id = self._task_store.append_message(
                target_id, role="user", content=response_text
            )
        except TaskStoreError:
            _logger.exception("orchestrator.forward_append_failed", task_id=target_id)
            return None

        # Surface the forwarded user reply on any open drawer for this task
        # so the transcript reflects the live multi-turn flow.
        try:
            for msg in self._task_store.get_task_messages(target_id):
                if msg.id != message_id:
                    continue
                await ws_events.emit(
                    {
                        "type": "task_message",
                        "task_id": target_id,
                        "message_id": msg.id,
                        "role": msg.role,
                        "content": msg.content,
                        "action": msg.action,
                        "created_at": msg.created_at,
                    }
                )
                break
        except TaskStoreError:
            _logger.exception("orchestrator.forward_emit_message_failed", task_id=target_id)

        await self._task_scheduler.resume(target_id)
        _logger.info("orchestrator.forwarded_to_subtask", task_id=target_id)
        return target_id

    async def _dispatch_cancel(self, call: Any) -> str | None:
        """Route a ``cancel_subtask`` tool call to the scheduler.

        The scheduler is permissive: cancelling an unknown / terminal task
        is a no-op, so we don't pre-validate the task_id here. We only
        validate the argument shape so a malformed call doesn't crash the
        orchestrator turn.

        The reason — when omitted by Jarvis — defaults to ``user_cancelled``
        to match the WS sidebar path; Jarvis may override with a brief
        contextual reason ("trop long", "plus utile", …).
        """

        target_id = call.arguments.get("task_id")
        if not isinstance(target_id, str) or not target_id.strip():
            _logger.warning("orchestrator.cancel_bad_task_id", arguments=call.arguments)
            return None

        raw_reason = call.arguments.get("reason")
        reason = (
            raw_reason.strip()
            if isinstance(raw_reason, str) and raw_reason.strip()
            else "user_cancelled"
        )

        await self._task_scheduler.cancel(target_id, reason=reason)
        _logger.info(
            "orchestrator.cancelled_subtask",
            task_id=target_id,
            reason=reason,
        )
        return target_id

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
            forwarded_task_ids=[],
        )


# Process-wide singleton. Slice #0025 turns the per-call factory into a true
# cached instance so the WS layer, the proactivity handler and the lifespan
# all share the same ``_jarvis_state`` / ``_user_typing`` / queue state.
# ``set_default_orchestrator`` is exposed for tests so they can install a
# narrow double when needed.
_DEFAULT_ORCHESTRATOR: Orchestrator | None = None


def set_default_orchestrator(orchestrator: Orchestrator | None) -> None:
    """Install (or clear) the process-wide singleton :class:`Orchestrator`."""

    global _DEFAULT_ORCHESTRATOR
    _DEFAULT_ORCHESTRATOR = orchestrator


def get_default_orchestrator() -> Orchestrator:
    """Return the cached :class:`Orchestrator` singleton, building it on first use.

    The instance is cached so the WS handler, the proactivity handler and
    the lifespan flusher all share the same buffering state. Tests that need
    a clean slate can call :func:`set_default_orchestrator(None)` to discard
    the cached instance, or install a narrow double directly.

    Relies on the singletons primed by :func:`bob.main.lifespan` — the
    :class:`TaskStore`, the :class:`TaskScheduler` (already wired with its
    sub-agent runner factory at boot), and the Jarvis prompt loader. Tests
    that bypass the lifespan should use the DI constructor directly and
    install the result via :func:`set_default_orchestrator`.
    """

    global _DEFAULT_ORCHESTRATOR
    if _DEFAULT_ORCHESTRATOR is not None:
        return _DEFAULT_ORCHESTRATOR

    from bob.jarvis_prompt_loader import load_jarvis_prompt
    from bob.llm.factory import build_jarvis_client

    settings = get_settings()
    _DEFAULT_ORCHESTRATOR = Orchestrator(
        jarvis_client=build_jarvis_client(settings),
        jarvis_store=jarvis_store_module.get_default_store(),
        task_store=task_store_module.get_default_store(),
        task_scheduler=task_scheduler.get_default_scheduler(),
        jarvis_prompt=load_jarvis_prompt(settings.BOB_DATA_DIR),
    )
    return _DEFAULT_ORCHESTRATOR
