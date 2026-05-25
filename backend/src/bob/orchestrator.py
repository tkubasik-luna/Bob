"""Jarvis orchestrator — user-turn entry point and sub-task dispatcher.

``Orchestrator.process_user_message`` is the single user-message entry point
used by the WebSocket layer and the smoke CLI. PRD 0006 / issue 0047
unified every Jarvis emission as a tool call — there is no longer a
free-form text path. The orchestrator:

1. Records the user message in the persistent Jarvis thread.
2. Builds the system prompt from versioned :mod:`prompt_fragments` and
   assembles the chat-messages list via :class:`ContextAssembler`.
3. Invokes :meth:`LLMClient.complete` in tool-call mode with the
   versioned tool registry (``say`` + ``spawn_subtask`` +
   ``forward_to_subtask`` + ``cancel_subtask``).
4. Dispatches every tool call through :class:`ToolDispatcher`:
   - ``say`` — the unified direct-reply path. The handler persists the
     assistant turn and threads ``speech`` + optional ``ui`` back into
     the :class:`OrchestratorResponse` so the WS router emits a single
     ``assistant_msg`` frame, unchanged from the legacy contract.
   - ``spawn_subtask`` / ``forward_to_subtask`` / ``cancel_subtask`` —
     side-effect tools. The orchestrator emits the matching versioned
     confirmation phrase and persists it in the Jarvis thread.

The structured-output ``chat()`` + ``response_parser`` path was removed
in 0047. When the LLM violates the tool-call contract (no tool call
returned, or every dispatch errored) the orchestrator raises
:class:`OrchestratorContractError`. The WS router surfaces this as an
``INTERNAL`` error frame today; the per-tool retry/degrade policy that
ships in 0048 will catch it earlier and emit a hardcoded fallback
``say("Désolé, peux-tu reformuler ?")``.

Slice #0021 added :meth:`generate_proactive_message`: when a sub-agent
emits ``ask_user`` the :class:`ProactivityHandler` invokes this method.
Jarvis paraphrases the raw question in his own tone and the orchestrator
pushes a single ``assistant_msg`` with ``proactive: true`` back through the
WS emitter — no user turn triggered it. The proactivity path still uses
``LLMClient.chat()`` directly because it is a templated single-turn
synthesis, not a user-driven turn.

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
from bob import task_scheduler, ws_events
from bob import task_store as task_store_module
from bob import ui_registry as ui_registry_module
from bob.config import get_settings
from bob.context.assembler import ContextAssembler
from bob.context.policy import (
    BOUNDED_V1_POLICY_ID,
    BOUNDED_V2_POLICY_ID,
    LEGACY_FULL_HISTORY_POLICY_ID,
    ContextPolicy,
    bounded_v2_policy,
)
from bob.context.prompt_fragments import (
    ASK_USER_PARAPHRASE_TEMPLATE,
    CANCEL_CONFIRMATION,
    DONE_SYNTHESIS_TEMPLATE,
    FORWARD_CONFIRMATION,
    SPAWN_CONFIRMATION,
    TOOLS_SYSTEM_ADDENDUM,
)
from bob.context.providers.cross_epoch_digest import CrossEpochDigestProvider
from bob.context.providers.legacy_full_history import LegacyFullHistoryProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.system_block import SystemBlockProvider
from bob.context.providers.user_message import UserMessageProvider
from bob.context.summariser import LLMSummariser, Summariser
from bob.context.summary_pipeline import maybe_regenerate_rolling_summary
from bob.debug_log import emit_debug, start_turn
from bob.epoch import (
    CrossEpochDigestStore,
    EpochManager,
    EpochPolicy,
    RetrievalAPI,
)
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


class OrchestratorContractError(RuntimeError):
    """Raised when a Jarvis turn violates the "exactly one tool call" contract.

    PRD 0006 / issue 0047 unified every Jarvis emission as a tool call.
    When the LLM returns plain text (no tool call) or every dispatched
    tool errors, the orchestrator raises this exception so the WS router
    surfaces it as an ``INTERNAL`` error frame today.

    Issue 0048 wires the retry/degrade policy: the dispatcher path will
    catch validation failures earlier and emit a hardcoded fallback
    ``say("Désolé, peux-tu reformuler ?")`` before this exception fires.
    Keeping a distinct exception type now means call sites can pattern
    match on contract violations rather than catching every ``Exception``.
    """


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
        epoch_policy: EpochPolicy | None = None,
        cross_epoch_digest_store: CrossEpochDigestStore | None = None,
        epoch_manager: EpochManager | None = None,
        retrieval_api: RetrievalAPI | None = None,
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
        # with a legacy policy; issue 0046 added the bounded v1 mix;
        # issue 0051 introduces :func:`bounded_v2_policy` (same mix plus
        # the cross-epoch digest) and switches the production default to
        # v2. The legacy policy stays available so tests and the byte-
        # for-byte regression snapshot can still target it explicitly.
        self._context_policy = context_policy or bounded_v2_policy()

        # PRD 0006 / issue 0046 — bounded policy needs a persistent rolling
        # summary. The orchestrator owns the store + the summariser so the
        # WS layer never has to think about it.
        self._rolling_summary_store = rolling_summary_store
        self._summariser = summariser or LLMSummariser(chat=self._jarvis_chat_for_summary)

        # PRD 0006 / issue 0051 — sealed-epoch plumbing. The orchestrator
        # owns the digest store, the :class:`EpochManager` (token-threshold
        # sealer) and the :class:`RetrievalAPI` stub. Lazy-init mirrors
        # the rolling-summary store pattern so legacy tests that bypass
        # ``main.lifespan`` still construct a functional orchestrator.
        self._epoch_policy = epoch_policy or EpochPolicy()
        self._cross_epoch_digest_store = cross_epoch_digest_store
        self._epoch_manager_cached = epoch_manager
        self._retrieval_api = retrieval_api or RetrievalAPI()

        # PRD 0006 / issue 0044 — Jarvis-side tools are now dispatched
        # through a versioned :class:`ToolRegistry`. The default registry
        # registers ``say`` (issue 0047) + ``spawn_subtask`` /
        # ``forward_to_subtask`` / ``cancel_subtask``. Tests can inject a
        # narrower registry to exercise the dispatcher in isolation.
        #
        # Issue 0047 threads the orchestrator's :class:`JarvisStore` into
        # the :class:`ToolHandlerContext` so the ``say`` handler can
        # persist the assistant turn through the same DI bag as every
        # other handler.
        self._tool_registry = tool_registry or build_default_registry()
        self._tool_dispatcher = ToolDispatcher(
            registry=self._tool_registry,
            context=ToolHandlerContext(
                task_store=self._task_store,
                task_scheduler=self._task_scheduler,
                ws_emit=ws_events.emit,
                jarvis_store=self._jarvis_store,
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

            # PRD 0006 / issue 0051 — observable retrieval read path.
            # ``recall`` returns ``[]`` at v1 but logs the call so the
            # sealed-epoch logic cannot rot silently. The result is
            # currently unused; the v2 RAG implementation will inject
            # retrieved entries into the bounded prompt as a new
            # provider.
            self._trigger_retrieval(user_content)

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

            # PRD 0006 / issue 0047: every Jarvis turn ends in exactly
            # one dispatched tool call. ``decision.is_tool_call`` must be
            # true; the free-form text path was removed in 0047 and the
            # LLM-side system prompt explicitly forbids it. The retry /
            # degrade policy in 0048 will catch this earlier and emit a
            # hardcoded ``say("Désolé, peux-tu reformuler ?")`` fallback.
            if not decision.is_tool_call:
                _logger.warning(
                    "orchestrator.contract_violation_no_tool_call",
                    session_id=session_id,
                    text_preview=(decision.text or "")[:120],
                )
                raise OrchestratorContractError(
                    "Jarvis returned a free-form reply instead of a tool call. "
                    "Issue 0048 will degrade this to a hardcoded say() fallback."
                )

            response = await self._dispatch_tool_calls(decision.tool_calls)
            if response is None:
                # Every tool call errored. Same shape as the no-tool-call
                # path — 0048 will degrade this to the hardcoded ``say()``
                # fallback through the same dispatcher.
                _logger.warning(
                    "orchestrator.contract_violation_all_dispatches_errored",
                    session_id=session_id,
                    tool_call_count=len(decision.tool_calls),
                )
                raise OrchestratorContractError(
                    "Every Jarvis tool call failed dispatch. "
                    "Issue 0048 will degrade this to a hardcoded say() fallback."
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
                    "spawned_task_ids": response.spawned_task_ids,
                    "forwarded_task_ids": response.forwarded_task_ids,
                    "cancelled_task_ids": response.cancelled_task_ids,
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

        # Bounded providers. Both ``bounded_v1`` (no cross-epoch digest)
        # and ``bounded_v2`` (with digest) wire the same store-bound
        # providers; the assembler picks via the policy's
        # ``provider_ids`` so unused providers are simply not invoked.
        current_epoch_id = self._current_epoch_id_for_assembly()
        system_provider = SystemBlockProvider(system_content=system_content)
        digest_provider = CrossEpochDigestProvider(store=self._ensure_digest_store())
        rolling_provider = RollingSummaryProvider(
            store=self._ensure_summary_store(),
            current_epoch_id=current_epoch_id,
        )
        recent_provider = RecentTurnsProvider(jarvis_store=self._jarvis_store)
        user_provider = UserMessageProvider()
        assembler = ContextAssembler(
            providers=[
                system_provider,
                digest_provider,
                rolling_provider,
                recent_provider,
                user_provider,
            ],
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

    def _ensure_digest_store(self) -> CrossEpochDigestStore:
        """Lazy-init the cross-epoch digest store (issue 0051)."""

        if self._cross_epoch_digest_store is None:
            conn = self._jarvis_store._conn
            self._cross_epoch_digest_store = CrossEpochDigestStore(conn)
        return self._cross_epoch_digest_store

    def _current_epoch_id_for_assembly(self) -> int:
        """Read the live ``current_epoch_id`` for provider construction.

        Under ``bounded_v2`` the orchestrator's :class:`EpochManager`
        owns the live epoch id. Under any other policy we default to
        ``0`` so the rolling-summary provider keeps its pre-0051
        behavior (read the latest row regardless of epoch).
        """

        if self._context_policy.policy_id != BOUNDED_V2_POLICY_ID:
            return 0
        return self._ensure_epoch_manager().current_epoch_id

    def _ensure_epoch_manager(self) -> EpochManager:
        """Lazy-init the :class:`EpochManager` (issue 0051).

        The manager is bound to the rolling-summary store + the digest
        store + the live SQLite connection so :meth:`apply_seal` can
        read RAW sealed turns directly. Tests inject a pre-built
        manager via the constructor when they need a low-threshold
        policy.
        """

        if self._epoch_manager_cached is None:
            conn = self._jarvis_store._conn
            self._epoch_manager_cached = EpochManager(
                policy=self._epoch_policy,
                rolling_summary_store=self._ensure_summary_store(),
                digest_store=self._ensure_digest_store(),
                conn=conn,
            )
        return self._epoch_manager_cached

    async def _maybe_regenerate_summary(self) -> None:
        """Regenerate the rolling summary if the older slice has grown enough.

        No-op when:

        * The active policy is not a bounded policy (``bounded_v1`` /
          ``bounded_v2``).
        * The persisted history is too short — :func:`maybe_regenerate_rolling_summary`
          returns ``None`` and the rolling-summary block stays empty.
        * The summariser raises — the failure is logged and swallowed so a
          single bad summarisation never breaks the live turn.

        Under ``bounded_v2`` (PRD 0006 / issue 0051) we also evaluate the
        :class:`EpochManager` token-threshold trigger immediately after
        the regeneration: if the freshly persisted summary's token count
        crossed the threshold, the manager seals the epoch and rebuilds
        the cross-epoch digest from RAW sealed turns. Both calls live
        on the same code path so the seal lifecycle stays observable
        from one orchestrator hook.
        """

        if self._context_policy.policy_id not in (BOUNDED_V1_POLICY_ID, BOUNDED_V2_POLICY_ID):
            return
        recent_window = self._context_policy.recent_turns_window or 3
        current_epoch_id = self._current_epoch_id_for_assembly()
        try:
            await maybe_regenerate_rolling_summary(
                jarvis_store=self._jarvis_store,
                summary_store=self._ensure_summary_store(),
                summariser=self._summariser,
                recent_window=recent_window,
                current_epoch_id=current_epoch_id,
            )
        except Exception:
            _logger.exception("orchestrator.rolling_summary_failed")

        if self._context_policy.policy_id == BOUNDED_V2_POLICY_ID:
            try:
                self._ensure_epoch_manager().apply_seal()
            except Exception:
                _logger.exception("orchestrator.epoch_seal_failed")

    def _trigger_retrieval(self, user_content: str) -> None:
        """Observable call site for the retrieval read path (PRD 0006 / issue 0051).

        The result is currently unused — :meth:`RetrievalAPI.recall` is
        a v1 stub returning ``[]`` — but the call MUST happen so the
        ``retrieval.recall_called`` structured-log event flows on every
        turn. Without an active read path the sealed-epoch logic rots
        silently; see PRD "Further Notes". When real RAG ships the
        body changes; the call site stays.
        """

        try:
            self._retrieval_api.recall(user_content)
        except Exception:
            # Never let the read-path stub take down a live turn. Real
            # retrieval will have its own retry/degrade policy under
            # 0048; for now we swallow and log.
            _logger.exception("orchestrator.retrieval_recall_failed")

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

    async def _dispatch_tool_calls(self, tool_calls: list[ToolCall]) -> OrchestratorResponse | None:
        """Dispatch every tool call through the :class:`ToolDispatcher`.

        Returns the assembled :class:`OrchestratorResponse` describing
        the turn, or ``None`` when every dispatch errored — the caller
        then routes through the contract-violation path.

        PRD 0006 / issue 0047 unified Jarvis emission: direct replies
        become a ``say`` tool call (the handler persists the assistant
        turn and threads the spoken text + optional UI back through the
        :class:`DispatchResult`); task operations remain
        ``spawn_subtask`` / ``forward_to_subtask`` / ``cancel_subtask``
        (each surfaces its versioned confirmation fragment as the
        spoken reply). The prompt forbids multiple tool calls in a
        single turn, but if the LLM violates that constraint we keep
        the *first* ``say`` and apply the legacy dominant-action
        priority for task tools (cancel → forward → spawn).
        """

        spawned: list[str] = []
        forwarded: list[str] = []
        cancelled: list[str] = []
        say_speech: str | None = None
        say_ui: Any = None
        any_ok = False
        for call in tool_calls:
            result = await self._tool_dispatcher.dispatch(call)
            if not result.ok:
                continue
            any_ok = True
            self._collect_dispatch_result(result, spawned, forwarded, cancelled)
            if result.tool_name == "say" and say_speech is None:
                say_speech = result.speech
                say_ui = result.ui

        if not any_ok:
            return None

        # Task confirmations take precedence over a coexisting ``say``
        # call (the prompt forbids both — the priority matches the
        # pre-0047 dominant-action heuristic so observed behavior on
        # spawn / forward / cancel turns is unchanged).
        if cancelled or forwarded or spawned:
            if cancelled:
                speech = _CANCEL_CONFIRMATION
            elif forwarded:
                speech = _FORWARD_CONFIRMATION
            else:
                speech = _SPAWN_CONFIRMATION
            self._jarvis_store.append("assistant", speech)
            return OrchestratorResponse(
                speech=speech,
                ui=[],
                spawned_task_ids=spawned,
                forwarded_task_ids=forwarded,
                cancelled_task_ids=cancelled,
            )

        # Pure ``say`` turn. The handler already persisted the assistant
        # row in :class:`JarvisStore`; here we only lift speech + ui
        # into the response shape the WS router consumes.
        assert say_speech is not None
        return OrchestratorResponse(
            speech=say_speech,
            ui=_coerce_say_ui(say_ui),
            spawned_task_ids=[],
            forwarded_task_ids=[],
            cancelled_task_ids=[],
        )

    def _collect_dispatch_result(
        self,
        result: DispatchResult,
        spawned: list[str],
        forwarded: list[str],
        cancelled: list[str],
    ) -> None:
        """Append ``result.task_id`` to the right bucket on a successful dispatch.

        Each task tool maps to exactly one of the three response lists.
        The dispatcher already emitted the ``jarvis.route`` event so
        this helper stays narrowly focused on the legacy response
        shape. ``say`` carries no ``task_id``; the caller collects its
        speech + ui side payload directly off the
        :class:`DispatchResult`.
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


def _coerce_say_ui(ui: Any) -> list[ComponentDescriptor]:
    """Normalise the ``say.ui`` argument into the orchestrator's UI shape.

    Pre-0047 the structured-output path validated ``ui`` against the
    full :mod:`bob.ui_registry` JSON schema. Issue 0047 keeps the
    contract permissive at the tool boundary (the LLM may emit ``null``
    most of the time and an opaque ``{component, props}`` object when
    a Markdown overlay is warranted); validation against the registry's
    versioned schema lands with 0048's per-tool retry/degrade. Until
    then we coerce best-effort:

    * ``None`` → empty list (the common case).
    * A dict with ``component`` and ``props`` → wrap into a single
      :class:`ComponentDescriptor` (legacy structured-output shape).
    * Any other shape → empty list + a warning log so misuse is loud
      without breaking the live turn.
    """

    if ui is None:
        return []
    if isinstance(ui, dict):
        component = ui.get("component")
        props = ui.get("props", {})
        if isinstance(component, str) and isinstance(props, dict):
            try:
                return [ComponentDescriptor(component=component, props=props)]
            except Exception:
                _logger.warning("orchestrator.say_ui_invalid_component", ui=ui)
                return []
    _logger.warning("orchestrator.say_ui_unexpected_shape", ui=ui)
    return []


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
