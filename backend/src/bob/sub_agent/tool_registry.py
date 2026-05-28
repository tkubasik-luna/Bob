"""Sub-agent-side :class:`ToolRegistry` + :class:`ToolDispatcher`.

The Jarvis-side registry under :mod:`bob.tools` is reused here as the
*shape* but **not** as the same instance: a sub-agent's tool surface is
strictly disjoint from Jarvis's (no ``spawn_subtask`` / ``say`` on a
sub-agent — it would let a sub-agent spawn its own children, which is
explicitly out of scope per PRD 0006 / "Out of scope").

Tool definitions live here:

- ``gmail_search(...)`` (issue 0055) — first real tool wired in. Bridges
  the sub-agent runtime to :mod:`bob.connectors.gmail` so research
  sub-tasks can answer email-lookup goals and feed the ``Mail`` UI
  component.
- ``web_search(query)`` / ``web_fetch(url)`` — historical placeholders
  whose handlers still raise ``NotImplementedError``. Builders remain
  available for the day a real HTTP backend lands; the default registry
  does not register them because advertising a never-succeeding tool
  wastes an LLM round-trip per research task.

The :class:`SubAgentToolHandlerContext` mirrors
:class:`bob.tools.dispatcher.ToolHandlerContext` but for the sub-agent
domain: the only dependency surfaced today is a free-form ``state``
dict the runner can stash per-call data into (LLM client handle,
correlation id, …). This stays minimal because individual tool
implementations are placeholders.

A separate :class:`SubAgentToolDispatcher` runs each call: it looks the
tool up in the registry, Pydantic-validates the args, invokes the
handler, and folds the outcome into a structured
:class:`SubAgentToolDispatchResult`. Symmetric to
:class:`bob.tools.dispatcher.ToolDispatcher` so 0048 can plug the same
``on_validation_exhausted`` policy into both registries.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, Field, ValidationError, model_validator

_logger = structlog.get_logger(__name__)


class SubAgentToolArgsValidationError(ValueError):
    """Raised when a sub-agent tool call's arguments fail Pydantic validation."""

    def __init__(self, *, tool_name: str, message: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.message = message


@dataclass(frozen=True)
class SubAgentToolHandlerOutcome:
    """Result of a sub-agent tool handler invocation."""

    status: Literal["ok", "error"]
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class SubAgentToolHandlerContext(Protocol):
    """Dependency bag surfaced to sub-agent tool handlers.

    Kept Protocol-shaped (not a frozen dataclass) so tests can plug a
    lightweight stub without instantiating the full runner. The runner
    populates ``task_id`` + ``state`` when it dispatches.
    """

    @property
    def task_id(self) -> str: ...

    @property
    def state(self) -> dict[str, Any]: ...


SubAgentToolHandler = Callable[
    [SubAgentToolHandlerContext, BaseModel],
    Awaitable[SubAgentToolHandlerOutcome],
]


@dataclass(frozen=True)
class SubAgentToolDefinition:
    """Versioned tool the sub-agent runner can dispatch.

    Mirrors :class:`bob.tools.registry.ToolDefinition` but lives in a
    separate dataclass so the two registries cannot accidentally be
    merged (PRD: sub-agent context ≠ Jarvis context).
    """

    name: str
    version: str
    description: str
    args_model: type[BaseModel]
    handler: SubAgentToolHandler

    @property
    def qualified_name(self) -> str:
        """Return the canonical ``"v1.web_search"`` identifier."""

        return f"{self.version}.{self.name}"


class SubAgentToolRegistry:
    """Ordered, name-indexed collection of :class:`SubAgentToolDefinition`."""

    def __init__(self, definitions: list[SubAgentToolDefinition] | None = None) -> None:
        self._definitions: list[SubAgentToolDefinition] = []
        self._by_name: dict[str, SubAgentToolDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: SubAgentToolDefinition) -> None:
        if definition.name in self._by_name:
            raise ValueError(f"sub-agent tool already registered: {definition.name}")
        self._definitions.append(definition)
        self._by_name[definition.name] = definition

    def get(self, name: str) -> SubAgentToolDefinition | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return [d.name for d in self._definitions]

    def __iter__(self) -> Iterator[SubAgentToolDefinition]:
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)


@dataclass(frozen=True)
class SubAgentToolDispatchResult:
    """Outcome of one :meth:`SubAgentToolDispatcher.dispatch` call."""

    outcome: Literal["ok", "error"]
    tool_name: str
    tool_version: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class SubAgentToolDispatcher:
    """Validate + execute :class:`SubAgentToolDefinition` calls."""

    def __init__(self, registry: SubAgentToolRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> SubAgentToolRegistry:
        return self._registry

    async def dispatch(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        context: SubAgentToolHandlerContext,
    ) -> SubAgentToolDispatchResult:
        """Run one sub-agent tool call end-to-end.

        Unknown tool → ``error/unknown_tool``. Validation failure →
        ``error/invalid_args``. Handler exception → ``error/handler_failed``.
        Handler-reported error → ``error/<code>`` with the handler's
        ``error_code``. The dispatcher never raises; every path returns
        a :class:`SubAgentToolDispatchResult`.
        """

        definition = self._registry.get(name)
        if definition is None:
            return SubAgentToolDispatchResult(
                outcome="error",
                tool_name=name,
                tool_version=None,
                error_code="unknown_tool",
                error_message=f"unknown sub-agent tool: {name}",
            )

        try:
            validated = definition.args_model.model_validate(arguments)
        except ValidationError as exc:
            return SubAgentToolDispatchResult(
                outcome="error",
                tool_name=definition.name,
                tool_version=definition.version,
                error_code="invalid_args",
                error_message=str(exc),
            )

        try:
            outcome = await definition.handler(context, validated)
        except Exception as exc:
            return SubAgentToolDispatchResult(
                outcome="error",
                tool_name=definition.name,
                tool_version=definition.version,
                error_code="handler_failed",
                error_message=str(exc) or exc.__class__.__name__,
            )

        if outcome.status == "ok":
            return SubAgentToolDispatchResult(
                outcome="ok",
                tool_name=definition.name,
                tool_version=definition.version,
                result=outcome.result,
            )
        return SubAgentToolDispatchResult(
            outcome="error",
            tool_name=definition.name,
            tool_version=definition.version,
            error_code=outcome.error_code or "handler_failed",
            error_message=outcome.error_message,
        )


# ---------------------------------------------------------------------------
# Tool definitions — web_search + web_fetch placeholders.
# ---------------------------------------------------------------------------


class WebSearchArgs(BaseModel):
    """Validated arguments for ``web_search``."""

    query: str = Field(..., min_length=1, description="Search query string.")


class WebFetchArgs(BaseModel):
    """Validated arguments for ``web_fetch``."""

    url: str = Field(..., min_length=1, description="Absolute URL to fetch.")


async def _web_search_handler(
    _ctx: SubAgentToolHandlerContext,
    _args: BaseModel,
) -> SubAgentToolHandlerOutcome:
    """Placeholder — real HTTP call is intentionally deferred.

    Raises :class:`NotImplementedError` so a sub-agent that actually
    tries to call this tool surfaces the gap loudly rather than
    silently returning empty results. The dispatcher folds the
    exception into ``error/handler_failed``; tests stub the handler
    via a custom registry.
    """

    raise NotImplementedError("web_search is a placeholder; real backend lands in a later slice")


async def _web_fetch_handler(
    _ctx: SubAgentToolHandlerContext,
    _args: BaseModel,
) -> SubAgentToolHandlerOutcome:
    """Placeholder — symmetric to :func:`_web_search_handler`."""

    raise NotImplementedError("web_fetch is a placeholder; real backend lands in a later slice")


def build_web_search_tool() -> SubAgentToolDefinition:
    """Construct the registry entry for ``web_search`` (v1)."""

    return SubAgentToolDefinition(
        name="web_search",
        version="v1",
        description=(
            "Cherche le web et renvoie une liste de résultats (titre + extrait + url). "
            "Utilise pour des questions factuelles ou des recherches initiales."
        ),
        args_model=WebSearchArgs,
        handler=_web_search_handler,
    )


def build_web_fetch_tool() -> SubAgentToolDefinition:
    """Construct the registry entry for ``web_fetch`` (v1)."""

    return SubAgentToolDefinition(
        name="web_fetch",
        version="v1",
        description=(
            "Récupère le contenu textuel d'une URL pour analyse approfondie. "
            "Utilise après ``web_search`` quand un résultat mérite d'être lu en entier."
        ),
        args_model=WebFetchArgs,
        handler=_web_fetch_handler,
    )


# ---------------------------------------------------------------------------
# Tool definition — gmail_search (issue 0055).
# ---------------------------------------------------------------------------


_GMAIL_SEARCH_MAX_RESULTS_CAP = 5


class GmailSearchArgs(BaseModel):
    """Validated structured arguments for ``gmail_search``.

    Mirrors the keyword surface of
    :func:`bob.connectors.gmail.query_builder.build_query` so the sub-agent
    LLM can express a precise lookup without ever touching Gmail's raw
    operator syntax. ``max_results`` is hard-capped at 5 server-side: the
    Mail overlay shows one card at a time, asking the LLM to triage more
    than a handful is wasted tokens.

    Validation rules:

    - At least one of the seven filter fields must be set — an all-None
      payload would otherwise emit an empty query and Gmail returns the
      whole inbox, which is never the caller's intent.
    - ``max_results`` is clamped into ``[1, 5]`` so out-of-range values
      gracefully degrade instead of raising.
    """

    from_name: str | None = Field(
        default=None,
        description="Display name of the sender (e.g. 'Holyana Callejon').",
    )
    from_email: str | None = Field(
        default=None,
        description="Exact email address of the sender.",
    )
    subject_contains: str | None = Field(
        default=None,
        description="Substring the subject must contain.",
    )
    after: str | None = Field(
        default=None,
        description="ISO 8601 date — only mails received strictly after.",
    )
    before: str | None = Field(
        default=None,
        description="ISO 8601 date — only mails received strictly before.",
    )
    has_attachment: bool | None = Field(
        default=None,
        description="When true, restricts to messages carrying attachments.",
    )
    label: str | None = Field(
        default=None,
        description="Gmail label filter (e.g. 'INBOX', 'IMPORTANT').",
    )
    max_results: int = Field(
        default=1,
        ge=1,
        le=_GMAIL_SEARCH_MAX_RESULTS_CAP,
        description=("Maximum number of messages to return (1-5; cap enforced server-side)."),
    )

    @model_validator(mode="after")
    def _require_at_least_one_filter(self) -> GmailSearchArgs:
        """Reject all-None payloads — see class docstring."""

        any_filter = any(
            value is not None and (not isinstance(value, str) or value.strip())
            for value in (
                self.from_name,
                self.from_email,
                self.subject_contains,
                self.after,
                self.before,
                self.has_attachment,
                self.label,
            )
        )
        if not any_filter:
            raise ValueError(
                "gmail_search requires at least one filter "
                "(from_name / from_email / subject_contains / after / "
                "before / has_attachment / label); got all-None."
            )
        return self


async def _gmail_search_handler(
    _ctx: SubAgentToolHandlerContext,
    args: BaseModel,
) -> SubAgentToolHandlerOutcome:
    """Execute a Gmail search and surface ``to_mail_props`` dicts.

    The handler is the single point of integration between the sub-agent
    runtime and :mod:`bob.connectors.gmail`. It:

    1. Builds the Gmail ``q`` parameter from the validated structured
       arguments via :func:`query_builder.build_query`.
    2. Acquires refreshed credentials via :func:`auth.get_credentials`
       (silent refresh path; raises actionable errors when re-bootstrap
       is required).
    3. Calls :meth:`GmailClient.search_messages` and translates each
       :class:`EmailMessage` into the props dict the ``Mail`` UI
       component expects via :func:`to_mail_props`.

    Every exception path (missing token, refresh failure, Gmail API
    error, query build error) is folded into a structured ``error``
    outcome — the dispatcher contract is "never raise out of a handler".
    The sub-agent then decides how to surface the failure to the user
    (typically a plain ``say(speech=…)`` saying "no mail found" /
    "could not access Gmail").
    """

    assert isinstance(args, GmailSearchArgs)  # for mypy / runtime safety

    # Lazy import: the gmail connector pulls in google-auth which is
    # heavy to import; keeping it inside the handler means tool registry
    # construction stays cheap and unit tests for unrelated tools never
    # pay the import cost.
    from bob.connectors.gmail import (
        BootstrapRequiredError,
        GmailAuthError,
        GmailClient,
        QueryBuilderError,
        RefreshFailedError,
        auth,
        build_query,
        to_mail_props,
    )

    try:
        query = build_query(
            from_name=args.from_name,
            from_email=args.from_email,
            subject_contains=args.subject_contains,
            after=args.after,
            before=args.before,
            has_attachment=args.has_attachment,
            label=args.label,
        )
    except QueryBuilderError as exc:
        _logger.warning("gmail_search.query_build_failed", error=str(exc))
        return SubAgentToolHandlerOutcome(
            status="error",
            error_code="gmail_search_invalid_query",
            error_message=f"Invalid Gmail search arguments: {exc}",
        )

    try:
        credentials = auth.get_credentials()
    except BootstrapRequiredError as exc:
        _logger.warning("gmail_search.bootstrap_required", error=str(exc))
        return SubAgentToolHandlerOutcome(
            status="error",
            error_code="gmail_search_bootstrap_required",
            error_message=str(exc),
        )
    except RefreshFailedError as exc:
        _logger.warning("gmail_search.refresh_failed", error=str(exc))
        return SubAgentToolHandlerOutcome(
            status="error",
            error_code="gmail_search_refresh_failed",
            error_message=str(exc),
        )
    except GmailAuthError as exc:
        # Catch-all for the auth taxonomy — keeps the runtime resilient
        # if a new subclass lands without us updating the handler.
        _logger.warning("gmail_search.auth_failed", error=str(exc))
        return SubAgentToolHandlerOutcome(
            status="error",
            error_code="gmail_search_auth_failed",
            error_message=str(exc),
        )

    try:
        client = GmailClient(credentials)
        messages = client.search_messages(query, max_results=args.max_results)
    except Exception as exc:
        # ``googleapiclient`` raises a wide taxonomy (HttpError,
        # SocketTimeout, etc.). We do NOT depend on the concrete classes
        # here — surfacing a generic failure code keeps the connector
        # boundary clean and the handler unit-testable without HTTP
        # stubs. The dispatcher converts uncaught exceptions to
        # ``handler_failed``; we intercept first to attach a more useful
        # error_code for the LLM and downstream consumers.
        _logger.warning(
            "gmail_search.api_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return SubAgentToolHandlerOutcome(
            status="error",
            error_code="gmail_search_failed",
            error_message=f"Gmail search failed: {exc}",
        )

    return SubAgentToolHandlerOutcome(
        status="ok",
        result={
            "query": query,
            "count": len(messages),
            "messages": [to_mail_props(msg) for msg in messages],
        },
    )


def build_gmail_search_tool() -> SubAgentToolDefinition:
    """Construct the registry entry for ``gmail_search`` (v1).

    Description copy speaks to the LLM; keep it concise and operational —
    the sub-agent prompt fragment carries the longer guidance about
    meta-summary phrasing and emitting the result as a Mail overlay.
    """

    return SubAgentToolDefinition(
        name="gmail_search",
        version="v1",
        description=(
            "Recherche dans la boîte Gmail de l'utilisateur en combinant des "
            "filtres structurés (expéditeur, sujet, dates, etc.) et renvoie "
            "la liste des messages correspondants prête à être affichée par "
            "le composant ``Mail``. Utilise dès que la demande concerne un "
            "mail précis. ``max_results`` est limité à 5."
        ),
        args_model=GmailSearchArgs,
        handler=_gmail_search_handler,
    )


def build_default_subagent_registry() -> SubAgentToolRegistry:
    """Construct the default sub-agent tool registry.

    Currently exposes the ``gmail_search`` tool (issue 0055) so research
    sub-tasks can answer email-lookup goals. ``web_search`` / ``web_fetch``
    remain unwired — they raise ``NotImplementedError`` until a real HTTP
    backend lands. The builders stay available
    (:func:`build_web_search_tool` / :func:`build_web_fetch_tool`) and
    should be re-registered here once a real backend exists.

    Other slices may extend via :meth:`SubAgentToolRegistry.register` or by
    constructing a custom registry directly (tests do this).
    """

    return SubAgentToolRegistry([build_gmail_search_tool()])


__all__ = [
    "GmailSearchArgs",
    "SubAgentToolArgsValidationError",
    "SubAgentToolDefinition",
    "SubAgentToolDispatchResult",
    "SubAgentToolDispatcher",
    "SubAgentToolHandler",
    "SubAgentToolHandlerContext",
    "SubAgentToolHandlerOutcome",
    "SubAgentToolRegistry",
    "WebFetchArgs",
    "WebSearchArgs",
    "build_default_subagent_registry",
    "build_gmail_search_tool",
    "build_web_fetch_tool",
    "build_web_search_tool",
]
