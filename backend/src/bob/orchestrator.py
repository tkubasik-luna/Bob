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
from bob.context.policy import (
    BOUNDED_V1_POLICY_ID,
    LEGACY_FULL_HISTORY_POLICY_ID,
    ContextPolicy,
    bounded_v1_policy,
)
from bob.context.prompt_fragments import (
    ASK_USER_PARAPHRASE_TEMPLATE,
    CANCEL_CONFIRMATION,
    DONE_SYNTHESIS_TEMPLATE,
    FORWARD_CONFIRMATION,
    SPAWN_CONFIRMATION,
    TOOLS_SYSTEM_ADDENDUM,
)
from bob.context.providers.legacy_full_history import LegacyFullHistoryProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.system_block import SystemBlockProvider
from bob.context.providers.user_message import UserMessageProvider
from bob.context.summariser import LLMSummariser, Summariser
from bob.context.summary_pipeline import maybe_regenerate_rolling_summary
from bob.debug_log import emit_debug, start_turn
from bob.jarvis_store import JarvisStore
from bob.llm.types import ToolCall
from bob.llm_client import LLMClient
from bob.rolling_summary_store import RollingSummaryStore
from bob.task_store import TaskStore, TaskStoreError
from bob.tools import (
    DispatchResult,
    ToolDispatcher,
    ToolHandlerContext,
    ToolRegistry,
    build_default_registry,
)
from bob.ui_registry import ComponentDescriptor

_logger = structlog.get_logger(__name__)


# Legacy module-level constants kept as thin aliases over the versioned
# prompt fragments. Existing tests import some of these names; wording
# now lives in :mod:`bob.context.prompt_fragments` and is bumped via the
# ``version`` field on each :class:`PromptFragment`.
_SPAWN_CONFIRMATION = SPAWN_CONFIRMATION.template
_FORWARD_CONFIRMATION = FORWARD_CONFIRMATION.template
_CANCEL_CONFIRMATION = CANCEL_CONFIRMATION.template


_TOOLS_SYSTEM_ADDENDUM = TOOLS_SYSTEM_ADDENDUM.template


# Hard-coded template used by ``generate_proactive_message`` to paraphrase a
# sub-agent's ``ask_user`` question. Pinned in code (no `jarvis.md`-driven
# tuning). Issue 0046 lifted the literal into
# :data:`prompt_fragments.ASK_USER_PARAPHRASE_TEMPLATE`.
_ASK_USER_PARAPHRASE_TEMPLATE = ASK_USER_PARAPHRASE_TEMPLATE.template


# Slice #0025 + issue 0046: hard-coded template for ``generate_done_synthesis``.
# Now sourced from :data:`prompt_fragments.DONE_SYNTHESIS_TEMPLATE`.
_DONE_SYNTHESIS_TEMPLATE = DONE_SYNTHESIS_TEMPLATE.template


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
        tool_registry: ToolRegistry | None = None,
        rolling_summary_store: RollingSummaryStore | None = None,
        summariser: Summariser | None = None,
    ) -> None:
        self._jarvis_client = jarvis_client
        self._jarvis_store = jarvis_store
        self._task_store = task_store
        self._task_scheduler = task_scheduler
        self._jarvis_prompt = jarvis_prompt
        self._prompts = prompts
        self._ui_registry = ui_registry
        # PRD 0006 — every prompt the orchestrator sends is composed by
        # :class:`ContextAssembler`. Issue 0043 introduced the foundation
        # with a legacy policy; issue 0046 switches the production default
        # to :func:`bounded_v1_policy` (system + rolling summary + recent
        # turns + live user message). The legacy policy stays available so
        # tests and the byte-for-byte regression snapshot can still target
        # it explicitly.
        self._context_policy = context_policy or bounded_v1_policy()

        # PRD 0006 / issue 0046 — bounded policy needs a persistent rolling
        # summary. The orchestrator owns the store + the summariser so the
        # WS layer never has to think about it.
        self._rolling_summary_store = rolling_summary_store
        self._summariser = summariser or LLMSummariser(chat=self._jarvis_chat_for_summary)

        # PRD 0006 / issue 0044 — Jarvis-side tools are now dispatched
        # through a versioned :class:`ToolRegistry`. The default registry
        # registers ``spawn_subtask`` / ``forward_to_subtask`` /
        # ``cancel_subtask``. Tests can inject a narrower registry to
        # exercise the dispatcher in isolation.
        self._tool_registry = tool_registry or build_default_registry()
        self._tool_dispatcher = ToolDispatcher(
            registry=self._tool_registry,
            context=ToolHandlerContext(
                task_store=self._task_store,
                task_scheduler=self._task_scheduler,
                ws_emit=ws_events.emit,
            ),
        )

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

            # PRD 0006 / issue 0046 — bounded policy needs the rolling
            # summary to reflect the latest persisted older turns. The
            # pipeline only triggers an LLM call when the older slice has
            # grown past the threshold; otherwise it is a cheap read of
            # the persisted store. Legacy policy paths skip this entirely.
            await self._maybe_regenerate_summary()

            messages = self._assemble_chat_messages(
                system_content=complete_system,
                user_message=user_content,
            )

            decision = await self._jarvis_client.complete(
                messages,
                tools=self._tool_registry.as_llm_definitions(),
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
        Jarvis system prompt for the structured ``chat()`` path).

        Provider mix depends on the active :class:`ContextPolicy`:

        * ``legacy_full_history`` — wires the legacy provider so the
          assembled prompt is byte-equal with pre-0046 output. Used by the
          regression snapshot and by integration tests that pin the legacy
          policy explicitly.
        * ``bounded_v1`` (PRD 0006 / issue 0046, production default) —
          wires the bounded mix: system block + rolling summary + recent
          turns + live user message. The orchestrator persists the user
          turn before assembly, so ``RecentTurnsProvider`` trims the live
          row from its window and ``UserMessageProvider`` re-emits it as
          the trailing entry.
        """

        if self._context_policy.policy_id == LEGACY_FULL_HISTORY_POLICY_ID:
            provider = LegacyFullHistoryProvider(
                jarvis_store=self._jarvis_store,
                system_content=system_content,
            )
            assembler = ContextAssembler(providers=[provider], policy=self._context_policy)
            return assembler.assemble(user_message=user_message)

        # Bounded providers (default policy).
        system_provider = SystemBlockProvider(system_content=system_content)
        rolling_provider = RollingSummaryProvider(store=self._ensure_summary_store())
        recent_provider = RecentTurnsProvider(jarvis_store=self._jarvis_store)
        user_provider = UserMessageProvider()
        assembler = ContextAssembler(
            providers=[system_provider, rolling_provider, recent_provider, user_provider],
            policy=self._context_policy,
        )
        return assembler.assemble(user_message=user_message)

    def _ensure_summary_store(self) -> RollingSummaryStore:
        """Lazy-init the rolling-summary store on first use.

        The store is normally injected at construction (production wires
        it from the boot path); when callers omit it (legacy tests) we
        create an in-memory placeholder so the bounded provider mix stays
        functional. The placeholder reuses the Jarvis store's connection
        — migrations have already created the ``rolling_summaries`` table
        on every connection.
        """

        if self._rolling_summary_store is None:
            # Reach into JarvisStore for the connection — it is the same
            # connection migrations were applied against (see bob.main
            # lifespan + the integration test harness).
            conn = self._jarvis_store._conn
            self._rolling_summary_store = RollingSummaryStore(conn)
        return self._rolling_summary_store

    async def _maybe_regenerate_summary(self) -> None:
        """Regenerate the rolling summary if the older slice has grown enough.

        No-op when:

        * The active policy is not the bounded policy (``bounded_v1``).
        * The persisted history is too short — :func:`maybe_regenerate_rolling_summary`
          returns ``None`` and the rolling-summary block stays empty.
        * The summariser raises — the failure is logged and swallowed so a
          single bad summarisation never breaks the live turn.
        """

        if self._context_policy.policy_id != BOUNDED_V1_POLICY_ID:
            return
        recent_window = self._context_policy.recent_turns_window or 3
        try:
            await maybe_regenerate_rolling_summary(
                jarvis_store=self._jarvis_store,
                summary_store=self._ensure_summary_store(),
                summariser=self._summariser,
                recent_window=recent_window,
            )
        except Exception:
            _logger.exception("orchestrator.rolling_summary_failed")

    async def _jarvis_chat_for_summary(self, messages: list[dict[str, str]]) -> str:
        """Bridge between :class:`LLMSummariser` and the bound Jarvis client.

        The summariser only needs a "chat with messages, give me text"
        callable. Going through this method (rather than ``chat`` directly)
        keeps the summary path narrow + grep-friendly.
        """

        return await self._jarvis_client.chat(
            [dict(msg) for msg in messages],
            session_id="rolling_summary",
        )

    async def _dispatch_tool_calls(
        self, tool_calls: list[ToolCall]
    ) -> tuple[list[str], list[str], list[str]]:
        """Dispatch every tool call through the :class:`ToolDispatcher`.

        Returns ``(spawned, forwarded, cancelled)``. Each call is routed
        through the registry: unknown names and invalid argument shapes
        surface as :class:`DispatchResult(outcome="error", ...)`. The
        orchestrator currently mirrors the pre-0044 behavior — error
        outcomes simply do not contribute to any of the three lists, so
        the caller falls through to the chat path when every call failed
        (issue 0044 explicitly defers retry/degrade to 0048).
        """

        spawned: list[str] = []
        forwarded: list[str] = []
        cancelled: list[str] = []
        for call in tool_calls:
            result = await self._tool_dispatcher.dispatch(call)
            self._collect_dispatch_result(result, spawned, forwarded, cancelled)
        return spawned, forwarded, cancelled

    def _collect_dispatch_result(
        self,
        result: DispatchResult,
        spawned: list[str],
        forwarded: list[str],
        cancelled: list[str],
    ) -> None:
        """Append ``result.task_id`` to the right bucket on a successful dispatch.

        Each tool maps to exactly one of the three response lists. The
        dispatcher already emitted the ``jarvis.route`` event so this
        helper stays narrowly focused on the legacy response shape.
        """

        if not result.ok or result.task_id is None:
            return
        if result.tool_name == "spawn_subtask":
            spawned.append(result.task_id)
        elif result.tool_name == "forward_to_subtask":
            forwarded.append(result.task_id)
        elif result.tool_name == "cancel_subtask":
            cancelled.append(result.task_id)
        # Unknown-tool-on-success is structurally impossible (the
        # registry would have rejected the call upstream), but if a
        # future tool ships without an orchestrator branch the result is
        # silently ignored at this layer — the dispatcher still recorded
        # the route event.

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
