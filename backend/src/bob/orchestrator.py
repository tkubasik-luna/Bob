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
in 0047 — and ``response_parser`` itself was deleted in 0048 so the
silent raw-text fallback can never re-surface. When the LLM violates
the tool-call contract (no tool call returned, or every dispatch
errored) the per-tool retry/degrade policy
(:mod:`bob.validation`) injects validator feedback under the dedicated
``system_validator`` role for one retry; on budget exhaustion the
orchestrator runs ``on_validation_exhausted`` which dispatches the
hardcoded ``say("Désolé, peux-tu reformuler ?")`` through the live
:class:`ToolDispatcher`. The legacy :class:`OrchestratorContractError`
is kept for narrow cases (e.g. the dispatcher itself raises) so call
sites can pattern-match on the type rather than catching ``Exception``.

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
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

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
    FAILED_SYNTHESIS_TEMPLATE,
    FORWARD_CONFIRMATION,
    SPAWN_CONFIRMATION,
    TOOLS_SYSTEM_ADDENDUM,
)
from bob.context.providers.cross_epoch_digest import CrossEpochDigestProvider
from bob.context.providers.legacy_full_history import LegacyFullHistoryProvider
from bob.context.providers.recent_turns import RecentTurnsProvider
from bob.context.providers.rolling_summary import RollingSummaryProvider
from bob.context.providers.state_block import StateBlockProvider
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
from bob.llm.types import LLMResponse, ToolCall
from bob.llm_client import LLMClient
from bob.rolling_summary_store import RollingSummaryStore
from bob.streaming import StreamEmitter
from bob.task_completion_debouncer import (
    DEFAULT_DEBOUNCE_SECONDS,
    TaskCompletionDebouncer,
)
from bob.task_store import TaskStore, TaskStoreError
from bob.tools import (
    DispatchResult,
    ToolDispatcher,
    ToolHandlerContext,
    ToolRegistry,
    build_default_registry,
)
from bob.ui_registry import ComponentDescriptor
from bob.validation import (
    JARVIS_DEGRADE_SPEECH_FRAGMENT,
    CallEnvelope,
    ExhaustedContext,
    JarvisOnValidationExhausted,
    OnValidationExhausted,
    build_validator_message,
    get_policy,
    render_feedback,
)

if TYPE_CHECKING:  # pragma: no cover — typing-only.
    from bob.context.eviction import EvictionStrategy
    from bob.context.recency import RecencyPolicy
    from bob.context.state_policy import StatePolicy
    from bob.sub_agent.addendum_queue import AddendumQueue


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


# Template for the ``failed`` proactive synthesis. A sub-task that fails on
# its own (LLM error, timeout, …) must still come back to the user — pre-fix
# the proactivity handler silently ignored ``failed`` so the user was left
# hanging. User-initiated cancels do NOT reach this path (the scheduler's
# ``_finalize_cancelled`` emits no ``task_state_changed``).
_FAILED_SYNTHESIS_TEMPLATE = FAILED_SYNTHESIS_TEMPLATE.template


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
ProactiveEventKind = Literal["ask_user", "done", "failed"]


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
    - ``msg_id`` is the turn-stable id minted at the start of the
      :class:`bob.streaming.StreamEmitter` lifecycle (PRD 0006 / issue
      0049). The WS router uses it as the ``assistant_msg.msg_id`` so
      the streamed ``speech_delta`` frames the frontend already saw
      correlate with the final assistant bubble. Empty string when the
      turn was a degrade path that bypassed the streaming pipeline (the
      router falls back to its own generated id then).
    """

    speech: str
    ui: list[ComponentDescriptor] = field(default_factory=list)
    spawned_task_ids: list[str] = field(default_factory=list)
    forwarded_task_ids: list[str] = field(default_factory=list)
    cancelled_task_ids: list[str] = field(default_factory=list)
    msg_id: str = ""


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
        on_validation_exhausted: OnValidationExhausted | None = None,
        state_policy: StatePolicy | None = None,
        recency_policy: RecencyPolicy | None = None,
        eviction_strategy: EvictionStrategy | None = None,
        addendum_queue_factory: Callable[[str], AddendumQueue | None] | None = None,
        completion_debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
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
        # PRD 0006 / issue 0050 — wire the live runner registry (set
        # explicitly via :meth:`set_addendum_queue_factory` at boot) so the
        # ``addendum_task`` tool can resolve the per-task
        # :class:`AddendumQueue`. The orchestrator stays neutral to the
        # actual factory shape — tests inject a dict-backed double.
        self._addendum_queue_factory = addendum_queue_factory
        self._state_policy = state_policy
        self._recency_policy = recency_policy
        self._eviction_strategy = eviction_strategy

        self._tool_registry = tool_registry or build_default_registry()
        self._tool_dispatcher = ToolDispatcher(
            registry=self._tool_registry,
            context=ToolHandlerContext(
                task_store=self._task_store,
                task_scheduler=self._task_scheduler,
                ws_emit=ws_events.emit,
                jarvis_store=self._jarvis_store,
                addendum_queue_factory=self._addendum_queue_factory,
                mark_superseded=self._task_store.mark_superseded,
            ),
        )

        # PRD 0006 / issue 0050 — pending-completions debounce. The
        # orchestrator owns the debouncer so the WS layer never has to
        # think about batched announcements. Production wires the
        # ``task_state_changed`` subscriber to invoke
        # :meth:`enqueue_completion`. The flush callback materialises a
        # synthetic ``task_completed`` ContextEntry and pushes the
        # batched announcement via the proactive flusher.
        self._completion_debouncer = TaskCompletionDebouncer(
            flush_callback=self._on_completion_batch,
            window_seconds=completion_debounce_seconds,
        )

        # PRD 0006 / issue 0050 — monotonic user-turn counter consumed by
        # the STATE block provider (``age_turns`` + post-delivery
        # inclusion window) and by ``set_delivered_at_turn``.
        self._user_turn_index = 0

        # PRD 0006 / issue 0048 — degrade contract. The default handler
        # routes a hardcoded ``say("Désolé, peux-tu reformuler ?")``
        # through the live :class:`ToolDispatcher` so the side effects
        # (route event, JarvisStore persistence) match a regular turn.
        # Tests inject a recording double via the constructor.
        self._on_validation_exhausted: OnValidationExhausted = (
            on_validation_exhausted or JarvisOnValidationExhausted(dispatcher=self._tool_dispatcher)
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
        # PRD 0006 / issue 0050 — bump the monotonic user-turn counter
        # BEFORE the turn runs so the STATE block sees the new index in
        # ``age_turns`` and ``delivered_at_turn`` comparisons.
        self._user_turn_index += 1
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

            base_messages = self._assemble_chat_messages(
                system_content=complete_system,
                user_message=user_content,
            )

            response = await self._run_jarvis_turn_with_retry(
                base_messages=base_messages,
                session_id=session_id,
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

    async def _run_jarvis_turn_with_retry(
        self,
        *,
        base_messages: list[dict[str, Any]],
        session_id: str,
    ) -> OrchestratorResponse:
        """Drive one Jarvis turn through the per-tool retry policy.

        PRD 0006 / issue 0048 + 0049. The dispatcher path stays
        unchanged; the retry loop wraps it. Issue 0049 swaps the
        non-streaming :meth:`LLMClient.complete` for the streaming
        :meth:`LLMClient.stream_complete` so the user hears Jarvis
        start speaking while the LLM is still generating:

        1. Mint a turn-stable ``msg_id`` and build a fresh
           :class:`bob.streaming.StreamEmitter`. The emitter survives
           ACROSS retries on the same turn (each retry replaces it with
           a new instance + msg_id because the streamed deltas from a
           rejected turn have already been spoken — the next attempt
           must not collide with the previous msg_id).
        2. Run :meth:`LLMClient.stream_complete` with the current
           message list (initial: ``base_messages``; on retry:
           ``base_messages`` plus ``system_validator`` feedback messages
           appended).
        3. Pipe argument deltas through the emitter via
           :meth:`StreamEmitter.feed`. ``speech_delta`` frames flush
           to the WS bus during the loop. On ``tool_call_end`` we
           call :meth:`StreamEmitter.finalize` and collect the
           :class:`ToolCall` for dispatch.
        4. If no tool call was emitted → record the contract violation
           as feedback, increment the in-memory :class:`CallEnvelope`
           retry counter, loop.
        5. Otherwise dispatch through the existing path. If every
           dispatch errored → record feedback for the first error,
           increment the counter, loop.
        6. When ``envelope.retries_used`` exceeds the active per-tool
           :class:`RetryPolicy.max_retries` (or the no-tool-call path's
           default) → call
           :meth:`OnValidationExhausted.on_validation_exhausted`, then
           return an :class:`OrchestratorResponse` carrying the hardcoded
           degrade speech (and a generated ``msg_id`` so the WS router
           tags the assistant_msg consistently).

        The envelope dies at function exit — the retry counter NEVER
        lands on a :class:`ContextEntry`.

        Streaming + validation interaction: ``speech_delta`` frames are
        committed to the WS bus during the stream — they cannot be
        retro-cancelled. If the streamed ``say.speech`` validates but
        ``say.ui`` malforms, the ``say`` policy's
        ``accept_partial=True`` setting (see
        :data:`bob.validation.POLICY_TABLE`) accepts the call without
        emitting a ``ui_payload`` (overlay stays closed). If the LLM
        emits garbage that never resolves to a valid ``speech`` field,
        the retry path replaces the emitter and a fresh ``speech_delta``
        batch lands under a new msg_id — the user perceives a small
        false start. Rare in practice; see :mod:`bob.streaming.stream_emitter`
        for the long-form rationale.
        """

        envelope = CallEnvelope(tool_name=None, actor="jarvis")
        feedback_messages: list[dict[str, Any]] = []
        last_error_message = "validation failed"

        while True:
            messages = (
                base_messages
                if not feedback_messages
                else [
                    *base_messages,
                    *feedback_messages,
                ]
            )

            # PRD 0006 / issue 0049 — fresh emitter per attempt. The
            # msg_id binds the streamed deltas to the eventual
            # ``assistant_msg`` frame; a retry rolls a new id so the
            # frontend can distinguish the previous (rejected) speech
            # from the new attempt.
            attempt_msg_id = uuid.uuid4().hex
            emitter = StreamEmitter(msg_id=attempt_msg_id)
            decision = await self._stream_jarvis_call(
                messages=messages,
                session_id=session_id,
                emitter=emitter,
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
                    "attempt": envelope.attempts,
                    "msg_id": attempt_msg_id,
                },
            )

            # ---- No tool call → contract violation, retry if budget allows.
            if not decision.is_tool_call:
                last_error_message = (
                    "Tu n'as pas appelé d'outil. RÈGLE : chaque tour doit "
                    "être exactement UN appel d'outil. Pour répondre, appelle "
                    "``say`` avec ton texte dans ``speech``."
                )
                _logger.warning(
                    "orchestrator.contract_violation_no_tool_call",
                    session_id=session_id,
                    attempt=envelope.attempts,
                    text_preview=(decision.text or "")[:120],
                )
                emit_debug(
                    category="decision",
                    severity="warn",
                    source="orchestrator._run_jarvis_turn_with_retry",
                    summary=f"Violation contrat: aucun tool call (attempt {envelope.attempts})",
                    payload={
                        "session_id": session_id,
                        "attempt": envelope.attempts,
                        "retries_used": envelope.retries_used,
                        "text_preview": (decision.text or "")[:200],
                    },
                )
                # Default policy (no tool resolved) — the LLM did not
                # pick a tool yet so the policy table cannot key on a
                # name. Use the global default budget.
                policy = get_policy("say")
                if envelope.retries_used >= policy.max_retries:
                    return await self._degrade_validation_exhausted(
                        envelope=envelope,
                        last_error_message=last_error_message,
                    )
                feedback_messages.append(
                    build_validator_message(
                        render_feedback(
                            error_message=last_error_message,
                            offending_raw=decision.text,
                        )
                    )
                )
                envelope.record_feedback(feedback_messages[-1]["content"])
                envelope.increment(error_code="no_tool_call")
                continue

            # ---- Dispatch tool calls.
            response, dispatch_error = await self._dispatch_tool_calls_with_diagnostics(
                decision.tool_calls,
                msg_id=attempt_msg_id,
            )
            if response is not None:
                return response

            # Every dispatch errored — retry with feedback if budget allows.
            tool_name = dispatch_error.tool_name if dispatch_error is not None else None
            error_code = (
                dispatch_error.error_code if dispatch_error is not None else "dispatch_failed"
            )
            error_message = (
                dispatch_error.error_message
                if dispatch_error is not None and dispatch_error.error_message
                else "every tool call failed dispatch"
            )
            last_error_message = error_message
            envelope.tool_name = tool_name
            policy = get_policy(tool_name) if tool_name else get_policy("say")
            _logger.warning(
                "orchestrator.contract_violation_all_dispatches_errored",
                session_id=session_id,
                tool_call_count=len(decision.tool_calls),
                attempt=envelope.attempts,
                error_code=error_code,
            )
            emit_debug(
                category="decision",
                severity="warn",
                source="orchestrator._run_jarvis_turn_with_retry",
                summary=(
                    f"Violation contrat: dispatch {tool_name or '?'} échoué "
                    f"({error_code}, attempt {envelope.attempts})"
                ),
                payload={
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "error_code": error_code,
                    "error_message": error_message,
                    "attempt": envelope.attempts,
                    "retries_used": envelope.retries_used,
                },
            )
            if envelope.retries_used >= policy.max_retries:
                return await self._degrade_validation_exhausted(
                    envelope=envelope,
                    last_error_message=last_error_message,
                )
            feedback_messages.append(
                build_validator_message(
                    render_feedback(
                        error_message=(
                            f"Validation a échoué pour l'outil ``{tool_name}`` "
                            f"({error_code}): {error_message}. "
                            "Ré-essaye en respectant le schéma."
                        ),
                        offending_raw=None,
                    )
                )
            )
            envelope.record_feedback(feedback_messages[-1]["content"])
            envelope.increment(error_code=error_code)
            continue

    async def _stream_jarvis_call(
        self,
        *,
        messages: list[dict[str, Any]],
        session_id: str,
        emitter: StreamEmitter,
    ) -> LLMResponse:
        """Drive one streamed Jarvis LLM call through the :class:`StreamEmitter`.

        PRD 0006 / issue 0049. Bridges :meth:`LLMClient.stream_complete`
        (which yields :class:`StreamChunk`) and the legacy
        :class:`LLMResponse` shape the dispatcher path consumes.

        Per-chunk behaviour:

        - ``tool_call_start`` — register the call id + name. No emit
          yet (the emitter's first ``speech_delta`` carries the msg_id
          and the start phase is provider-internal).
        - ``tool_call_args_delta`` — feed the argument suffix into the
          emitter so a ``speech_delta`` can flush mid-stream. Also
          accumulate the suffix locally so we can fall back to
          re-parsing when ``final_arguments`` is missing.
        - ``tool_call_end`` — finalise the emitter (which fires
          ``ui_payload`` when applicable) and stash the call in the
          collected :class:`ToolCall` list.
        - ``text`` — accumulate into a local text buffer; the dispatcher
          path will surface it as a contract violation if no tool call
          followed.

        We only feed argument deltas into the emitter for the FIRST
        ``say`` call: ``ui_payload`` + ``speech_delta`` are exclusively
        ``say`` concerns. Calls to other tools (``spawn_subtask``,
        ``forward_to_subtask``, ``cancel_subtask``) still stream
        through but their argument bytes are ignored by the emitter.
        """

        stream = await self._jarvis_client.stream_complete(
            messages,
            tools=self._tool_registry.as_llm_definitions(),
            session_id=session_id,
        )

        # Per-call accumulators keyed by ``tool_call_id``.
        in_flight: dict[str, dict[str, Any]] = {}
        # Order in which calls were started, so the final
        # :class:`LLMResponse` preserves provider order.
        call_order: list[str] = []
        # Local accumulator for plain-text path (text mode is rare under
        # the unified ``say`` tool).
        text_buffer = ""
        # Track which call is the active ``say`` — only one ``say`` may
        # stream per turn (PRD constraint). Subsequent ``say`` calls
        # would bypass the emitter; the dispatcher path enforces the
        # uniqueness.
        active_say_id: str | None = None
        emitter_finalised = False

        async for chunk in stream:
            if chunk.kind == "tool_call_start":
                assert chunk.tool_call_id is not None
                assert chunk.name is not None
                in_flight[chunk.tool_call_id] = {
                    "name": chunk.name,
                    "arguments_buffer": "",
                    "final_arguments": None,
                }
                call_order.append(chunk.tool_call_id)
                if chunk.name == "say" and active_say_id is None:
                    active_say_id = chunk.tool_call_id
            elif chunk.kind == "tool_call_args_delta":
                assert chunk.tool_call_id is not None
                state = in_flight.get(chunk.tool_call_id)
                if state is None:
                    # Defensive: provider streamed args before start —
                    # synthesise a stub state so we don't drop bytes.
                    state = {
                        "name": "",
                        "arguments_buffer": "",
                        "final_arguments": None,
                    }
                    in_flight[chunk.tool_call_id] = state
                    call_order.append(chunk.tool_call_id)
                state["arguments_buffer"] = cast(str, state["arguments_buffer"]) + chunk.args_delta
                if chunk.tool_call_id == active_say_id:
                    await emitter.feed(chunk.args_delta)
            elif chunk.kind == "tool_call_end":
                assert chunk.tool_call_id is not None
                state = in_flight.get(chunk.tool_call_id)
                if state is None:
                    # End without start — synthesise the slot.
                    state = {
                        "name": "",
                        "arguments_buffer": "",
                        "final_arguments": chunk.final_arguments,
                    }
                    in_flight[chunk.tool_call_id] = state
                    call_order.append(chunk.tool_call_id)
                else:
                    state["final_arguments"] = chunk.final_arguments
                if chunk.tool_call_id == active_say_id and not emitter_finalised:
                    emitter_finalised = True
                    await emitter.finalize(chunk.final_arguments)
            elif chunk.kind == "text":
                text_buffer += chunk.text_delta

        # Defensive: a stream that ends WITHOUT an explicit
        # ``tool_call_end`` for the active say leaves the emitter
        # un-finalised. Force a finalize so any pending ``ui_payload``
        # still fires from the accumulated buffer.
        if active_say_id is not None and not emitter_finalised:
            emitter_finalised = True
            buffered_state = in_flight.get(active_say_id, {})
            final_arguments = buffered_state.get("final_arguments")
            await emitter.finalize(final_arguments if isinstance(final_arguments, dict) else None)

        # Materialise the :class:`LLMResponse`. Tool calls take
        # precedence over plain-text content — the unified ``say`` tool
        # is the only legitimate spoken path, so even if the provider
        # surfaced both, the orchestrator routes through the tool
        # surface and the dispatcher persists the assistant turn.
        tool_calls: list[ToolCall] = []
        for call_id in call_order:
            state = in_flight[call_id]
            arguments: dict[str, Any]
            final = state.get("final_arguments")
            if isinstance(final, dict):
                arguments = cast(dict[str, Any], final)
            else:
                arguments_raw = cast(str, state.get("arguments_buffer", ""))
                if not arguments_raw.strip():
                    arguments = {}
                else:
                    try:
                        parsed = json.loads(arguments_raw)
                    except json.JSONDecodeError:
                        # Malformed final args — the validation retry
                        # loop will surface a contract violation.
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    arguments = cast(dict[str, Any], parsed)
            tool_calls.append(
                ToolCall(
                    id=call_id,
                    name=cast(str, state.get("name", "") or ""),
                    arguments=arguments,
                )
            )

        if tool_calls:
            return LLMResponse(text=None, tool_calls=tool_calls)
        return LLMResponse(text=text_buffer or None, tool_calls=[])

    async def _degrade_validation_exhausted(
        self,
        *,
        envelope: CallEnvelope,
        last_error_message: str,
    ) -> OrchestratorResponse:
        """Run the ``on_validation_exhausted`` handler + build the degrade response.

        The handler routes the hardcoded ``say()`` through the live
        dispatcher (so :class:`JarvisStore` persistence + the
        ``jarvis.route`` event still fire) and logs the structured
        ``jarvis.validation_failed`` event. We then return an
        :class:`OrchestratorResponse` carrying the same speech so the
        WS router emits exactly one ``assistant_msg`` frame.
        """

        await self._on_validation_exhausted.on_validation_exhausted(
            ExhaustedContext(
                envelope=envelope,
                last_error_message=last_error_message,
                task_id=None,
            )
        )
        # Observability: the degrade is the user-visible symptom ("Désolé,
        # peux-tu reformuler ?"). Surface it + the last error in the unified
        # debug log so the cause is one grep away (structlog warnings bypass
        # the debug bridge — see ws_router #3).
        emit_debug(
            category="decision",
            severity="error",
            source="orchestrator._degrade_validation_exhausted",
            summary="Budget retry épuisé → degrade 'Désolé, peux-tu reformuler ?'",
            payload={
                "tool_name": envelope.tool_name,
                "retries_used": envelope.retries_used,
                "attempts": envelope.attempts,
                "last_error_message": last_error_message,
            },
        )
        speech = JARVIS_DEGRADE_SPEECH_FRAGMENT.template
        return OrchestratorResponse(
            speech=speech,
            ui=[],
            spawned_task_ids=[],
            forwarded_task_ids=[],
            cancelled_task_ids=[],
        )

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

        if event_kind not in ("ask_user", "done", "failed"):
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

    # --- PRD 0006 / issue 0050 — completion batching + delivery -----------

    def set_addendum_queue_factory(
        self,
        factory: Callable[[str], AddendumQueue | None] | None,
    ) -> None:
        """Late-binding hook so the boot path can wire ``addendum_queue_factory``.

        Boot wiring needs the orchestrator instance *before* the scheduler /
        runner pool exists. The constructor accepts an optional factory for
        tests; production calls this method once the runner pool is alive.
        """

        self._addendum_queue_factory = factory
        # The dispatcher was constructed with the previous (possibly
        # ``None``) factory. Re-create the dispatcher's
        # :class:`ToolHandlerContext` so the new factory is visible to
        # the next dispatch.
        self._tool_dispatcher = ToolDispatcher(
            registry=self._tool_registry,
            context=ToolHandlerContext(
                task_store=self._task_store,
                task_scheduler=self._task_scheduler,
                ws_emit=ws_events.emit,
                jarvis_store=self._jarvis_store,
                addendum_queue_factory=self._addendum_queue_factory,
                mark_superseded=self._task_store.mark_superseded,
            ),
        )

    @property
    def user_turn_index(self) -> int:
        """Monotonic count of user turns seen by :meth:`process_user_message`."""

        return self._user_turn_index

    @property
    def completion_debouncer(self) -> TaskCompletionDebouncer:
        """Expose the per-orchestrator debouncer for tests + bus wiring."""

        return self._completion_debouncer

    async def enqueue_completion(self, task_id: str) -> None:
        """Register ``task_id`` for the next debounced delivery batch.

        Production wires the ``task_state_changed`` bus subscriber to call
        this whenever a sub-agent transitions to ``done`` (or
        ``failed`` / ``superseded``). Within the
        :attr:`TaskCompletionDebouncer.window_seconds` window every
        ``enqueue_completion`` call lands in the same batch; the flush
        callback (:meth:`_on_completion_batch`) materialises the
        synthetic ``task_completed`` :class:`ContextEntry` set + emits
        the proactive announcement.
        """

        await self._completion_debouncer.schedule(task_id)

    async def _on_completion_batch(self, task_ids: list[str]) -> None:
        """Flush callback: materialise + announce a batch of completed tasks.

        For every ``task_id`` in the batch we:

        1. Stamp ``delivered_at_turn`` so the same result is never
           announced twice (PRD acceptance criterion).
        2. Materialise a synthetic ``task_completed`` :class:`ContextEntry`
           into the Jarvis thread so subsequent turns see the result as
           part of history. Recency is *not* computed here — the next
           turn's STATE block recomputes it at assembly time per PRD.
        3. Push a single proactive announcement for the batch via the
           existing :meth:`generate_done_synthesis` flusher. With > 1
           task we let the flusher render each ``task_id`` in turn; a
           future enhancement can synthesise a single batched utterance.

        The handler swallows all exceptions per task — a single bad task
        must not block the batch.
        """

        for task_id in task_ids:
            try:
                self._task_store.set_delivered_at_turn(task_id, self._user_turn_index)
            except TaskStoreError:
                _logger.exception(
                    "orchestrator.completion_delivered_at_failed",
                    task_id=task_id,
                )
                continue
            try:
                task = self._task_store.get_task(task_id)
                # Materialise a synthetic ``task_completed`` row in the
                # Jarvis thread so the bounded recent-turns window can
                # carry the result forward. Recency is recomputed at the
                # next assembly per PRD.
                synthetic = (
                    f'[task_completed task_id={task_id} title="{task.title}" '
                    f"delivered_at_turn={self._user_turn_index}]"
                )
                self._jarvis_store.append("system", synthetic)
            except TaskStoreError:
                _logger.exception(
                    "orchestrator.completion_materialise_failed",
                    task_id=task_id,
                )
                continue
            # Schedule the spoken announcement through the same
            # proactivity flusher slice #0025 already wires. Multiple
            # task ids in a batch produce one announcement per task;
            # the user perceives them as back-to-back deliveries within
            # the same flush window.
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
                elif kind == "failed":
                    await self._do_generate_failed_synthesis(task_id)
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

    async def _do_generate_failed_synthesis(self, task_id: str) -> None:
        """Synthesise + push the ``failed`` announcement for ``task_id``.

        Mirrors :meth:`_do_generate_done_synthesis` but renders the failure
        template so Jarvis tells the user the task could not be completed and
        offers a recovery path, rather than leaving them waiting on a result
        that will never arrive.
        """

        try:
            task = self._task_store.get_task(task_id)
        except TaskStoreError:
            _logger.warning(
                "orchestrator.proactive_task_missing",
                task_id=task_id,
            )
            return

        result_text = task.result if task.result is not None else ""
        prompt = _FAILED_SYNTHESIS_TEMPLATE.format(
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
        # and ``bounded_v2`` (STATE + cross-epoch digest, issue 0050)
        # wire the same store-bound providers; the assembler picks via
        # the policy's ``provider_ids`` so unused providers are simply
        # not invoked.
        current_epoch_id = self._current_epoch_id_for_assembly()
        system_provider = SystemBlockProvider(system_content=system_content)
        state_provider = StateBlockProvider(
            task_store=self._task_store,
            state_policy=self._state_policy,
            recency_policy=self._recency_policy,
            eviction_strategy=self._eviction_strategy,
            current_user_turn=self._user_turn_index,
        )
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
                state_provider,
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
        """Dispatch every tool call; return the :class:`OrchestratorResponse` or ``None``.

        Thin wrapper kept for compatibility with the pre-0048 call
        sites; the retry path under :meth:`_run_jarvis_turn_with_retry`
        uses :meth:`_dispatch_tool_calls_with_diagnostics` which also
        exposes the first dispatch error for validator feedback.
        """

        response, _err = await self._dispatch_tool_calls_with_diagnostics(tool_calls)
        return response

    async def _dispatch_tool_calls_with_diagnostics(
        self,
        tool_calls: list[ToolCall],
        *,
        msg_id: str = "",
    ) -> tuple[OrchestratorResponse | None, DispatchResult | None]:
        """Dispatch every tool call through the :class:`ToolDispatcher`.

        Returns ``(response, None)`` when at least one dispatch succeeded
        and ``(None, first_error)`` when every dispatch errored. The
        first-error :class:`DispatchResult` is what the retry path
        feeds back to the LLM under the ``system_validator`` role.

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
        first_error: DispatchResult | None = None
        for call in tool_calls:
            result = await self._tool_dispatcher.dispatch(call)
            if not result.ok:
                if first_error is None:
                    first_error = result
                continue
            any_ok = True
            self._collect_dispatch_result(result, spawned, forwarded, cancelled)
            if result.tool_name == "say" and say_speech is None:
                say_speech = result.speech
                say_ui = result.ui

        if not any_ok:
            return None, first_error

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
            return (
                OrchestratorResponse(
                    speech=speech,
                    ui=[],
                    spawned_task_ids=spawned,
                    forwarded_task_ids=forwarded,
                    cancelled_task_ids=cancelled,
                    msg_id=msg_id,
                ),
                None,
            )

        # Pure ``say`` turn. The handler already persisted the assistant
        # row in :class:`JarvisStore`; here we only lift speech + ui
        # into the response shape the WS router consumes.
        if say_speech is None:
            # Defensive: a tool dispatched ``ok`` but was neither ``say``
            # nor a recognised task tool, so no speech was produced. Before
            # the v2-bucket fix this hit ``assert say_speech is not None``
            # and crashed the turn — and the crash was invisible (swallowed
            # by the ws_router catch-all + the structlog→debug bridge being
            # bypassed by ``PrintLoggerFactory``). Surface it loudly and
            # degrade to an empty reply instead of killing the turn.
            emit_debug(
                category="system",
                severity="error",
                source="orchestrator._dispatch_tool_calls_with_diagnostics",
                summary="dispatch ok but no speech produced (unbucketed tool?)",
                payload={
                    "tool_names": [call.name for call in tool_calls],
                    "spawned": spawned,
                    "forwarded": forwarded,
                    "cancelled": cancelled,
                },
            )
            return (
                OrchestratorResponse(
                    speech="",
                    ui=[],
                    spawned_task_ids=spawned,
                    forwarded_task_ids=forwarded,
                    cancelled_task_ids=cancelled,
                    msg_id=msg_id,
                ),
                None,
            )
        return (
            OrchestratorResponse(
                speech=say_speech,
                ui=_coerce_say_ui(say_ui),
                spawned_task_ids=[],
                forwarded_task_ids=[],
                cancelled_task_ids=[],
                msg_id=msg_id,
            ),
            None,
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
        # Bucket BOTH the legacy v1 names (``*_subtask``) and the v2 task
        # surface (``spawn_task`` / ``replan_task`` / ``cancel_task``,
        # PRD 0006 / issue 0050). ``forward_to_subtask`` is shared across
        # both versions. Pre-fix only the v1 names were recognised, so a
        # v2 ``spawn_task`` (the canonical entry point the prompt now
        # advertises) fell through every branch → ``spawned`` stayed empty
        # → the caller hit ``assert say_speech is not None`` and crashed
        # the whole turn, leaving the spawn unannounced.
        if result.tool_name in ("spawn_subtask", "spawn_task", "replan_task"):
            # ``replan_task`` cancels the old task and respawns a replacement;
            # the new task is the live one to confirm, so it shares the spawn
            # confirmation copy.
            spawned.append(result.task_id)
        elif result.tool_name == "forward_to_subtask":
            forwarded.append(result.task_id)
        elif result.tool_name in ("cancel_subtask", "cancel_task"):
            cancelled.append(result.task_id)
        # Unknown-tool-on-success is structurally impossible (the
        # registry would have rejected the call upstream). The caller's
        # ``say_speech is None`` guard now degrades gracefully (+ emits a
        # debug error) instead of crashing if a future tool ships without
        # an orchestrator branch here.


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
