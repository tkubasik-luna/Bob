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
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, Field, ValidationError, model_validator

from bob.llm.tooling import ToolSpec
from bob.sub_agent.result_store import ProjectedResult, ToolResultProjector

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
    #: PRD 0009 — pure ``result -> ProjectedResult`` hook owning how this
    #: tool's result becomes a compact transcript digest, a structured UI
    #: deliverable, a spoken summary, and whether it is a terminal answer.
    #: ``None`` (the default) routes the result through
    #: :func:`bob.sub_agent.result_store.default_projector`, i.e. pre-0009
    #: behaviour (full result in transcript, no card, never converges).
    result_projector: ToolResultProjector | None = None

    @property
    def qualified_name(self) -> str:
        """Return the canonical ``"v1.web_search"`` identifier."""

        return f"{self.version}.{self.name}"

    def to_spec(self) -> ToolSpec:
        """Project to the canonical :class:`bob.llm.tooling.ToolSpec`.

        Issue 0059 (PRD 0008). The sub-agent's argument surface is the
        single source of truth on ``args_model``; this routes it through
        :meth:`ToolSpec.from_args_model` so the prompt builder advertises
        the *real* argument JSON Schema (derived from Pydantic) instead of
        the legacy name+description-only line, and so a later self-
        correction phase (0062) can re-validate against ``spec.args_model``
        without re-plumbing the model. ``parameters`` is the model's
        ``model_json_schema()`` verbatim — schema flattening (``$defs`` /
        ``$ref`` inlining) is deferred to issue 0063.
        """

        return ToolSpec.from_args_model(
            name=self.name,
            description=self.description,
            args_model=self.args_model,
        )


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


#: Relative-date tokens the LLM may pass for ``after`` / ``before`` instead of
#: an absolute date. Resolved server-side against "now" so the value never
#: depends on the model knowing today's date (see issue: the model passed a
#: stale ``after:"2024-…"`` and stalled). Keys are matched case-insensitively.
_RELATIVE_DATE_TOKENS = {
    "today": 0,
    "aujourd'hui": 0,
    "aujourdhui": 0,
    "yesterday": -1,
    "hier": -1,
}


def _resolve_relative_date(value: str | None, *, today: date | None = None) -> str | None:
    """Resolve a relative-date token to a ``YYYY-MM-DD`` string.

    Returns the input unchanged when it is not a recognised token (absolute
    dates fall straight through to the query builder). ``today`` is injectable
    for deterministic tests; production passes ``None`` → ``datetime.now()``.
    """

    if value is None:
        return None
    token = value.strip().lower()
    delta = _RELATIVE_DATE_TOKENS.get(token)
    if delta is None:
        return value
    base = today or datetime.now().date()
    return (base + timedelta(days=delta)).strftime("%Y-%m-%d")


class GmailSearchArgs(BaseModel):
    """Validated structured arguments for ``gmail_search``.

    Mirrors the keyword surface of
    :func:`bob.connectors.gmail.query_builder.build_query` so the sub-agent
    LLM can express a precise lookup without ever touching Gmail's raw
    operator syntax. ``max_results`` has no upper cap — the Mail overlay
    renders one card per returned message, so any number the LLM asks for
    surfaces in full.

    Validation rules:

    - At least one of the seven filter fields must be set — an all-None
      payload would otherwise emit an empty query and Gmail returns the
      whole inbox, which is never the caller's intent.
    - ``max_results`` must be ``>= 1``; there is no upper bound.
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
        description=(
            "Date — only mails received strictly after. Accepts an ISO date "
            "('2026-05-30') or a relative token ('today'/'aujourd'hui', "
            "'yesterday'/'hier')."
        ),
    )
    before: str | None = Field(
        default=None,
        description=(
            "Date — only mails received strictly before. Accepts an ISO date "
            "('2026-05-30') or a relative token ('today'/'aujourd'hui', "
            "'yesterday'/'hier')."
        ),
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
        description=("Maximum number of messages to return (>= 1; no upper cap)."),
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
            after=_resolve_relative_date(args.after),
            before=_resolve_relative_date(args.before),
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

    # Issue 0056 — distinguish "Gmail API unreachable" (HTTP 5xx, quota,
    # network timeout) from other handler failures so the sub-agent can
    # produce a "Gmail down, try again later" speech rather than a generic
    # "search failed" message. The detection is best-effort and structural:
    # we look for the concrete ``HttpError`` raised by
    # ``googleapiclient.errors`` plus common ``OSError`` / ``TimeoutError``
    # subclasses that ``httplib2`` (the transport googleapiclient ships
    # with) bubbles up on socket failures. Anything that does not match
    # the unreachable taxonomy falls through to ``gmail_search_failed`` —
    # the LLM treats the two distinctly per the system prompt.
    try:
        client = GmailClient(credentials)
        messages = client.search_messages(query, max_results=args.max_results)
    except Exception as exc:
        # Metadata only in the warn log — message id / thread id / sender
        # never leak here because we never had them (the call failed before
        # any message decode). Subject / snippet are by construction absent
        # from the exception text.
        _logger.warning(
            "gmail_search.api_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        if _is_api_unreachable_exception(exc):
            return SubAgentToolHandlerOutcome(
                status="error",
                error_code="gmail_search_api_unreachable",
                error_message=f"Gmail API unreachable: {exc}",
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


def _is_api_unreachable_exception(exc: BaseException) -> bool:
    """Heuristic — does ``exc`` smell like a Gmail transport / API outage?

    Matches:

    - :class:`googleapiclient.errors.HttpError` — any HTTP failure from
      the Gmail API (5xx, quota, 401 from a revoked oauth scope, …). The
      sub-agent system prompt maps both 5xx and quota into the
      "réessaie dans un moment" speech; auth-revoked 401s would normally
      already surface as :class:`RefreshFailedError` upstream, but a
      raw 401 reaching the handler still routes to "unreachable" rather
      than the generic catch-all.
    - :class:`TimeoutError` and :class:`ConnectionError` — surface for
      pure socket-level failures (DNS down, host unreachable, slow
      response triggering a client-side timeout).

    Imported lazily so the handler module stays light when
    ``googleapiclient`` is not installed (e.g. a unit-test environment
    that stubs the connector boundary).
    """

    _HttpError: type[BaseException] | None
    try:
        from googleapiclient.errors import HttpError

        _HttpError = HttpError
    except Exception:  # pragma: no cover — defensive when googleapiclient missing
        _HttpError = None

    if _HttpError is not None and isinstance(exc, _HttpError):
        return True
    return isinstance(exc, TimeoutError | ConnectionError)


#: Per-message digest fields kept in the transcript. Deliberately EXCLUDES
#: ``bodyPreview`` (0056 privacy + PRD 0009 context-saving) and the heavy
#: ``attachments`` / id / url fields — the model only needs enough to know a
#: result exists and to write a one-line summary if convergence is off. The
#: full props (including ``bodyPreview``) live server-side in the store and are
#: rebuilt into the deliverable by code, never re-sent to the model.
_GMAIL_DIGEST_MESSAGE_FIELDS = ("subject", "receivedAt")
#: Cap on messages echoed into the digest so a ``max_results=5`` (or larger,
#: future) search cannot bloat the transcript. The deliverable carries one Mail
#: section per returned message regardless of this digest cap (issue 0067).
_GMAIL_DIGEST_MAX_MESSAGES = 5


def project_gmail_search(result: dict[str, Any]) -> ProjectedResult:
    """Project a ``gmail_search`` result into its transcript / UI / summary forms.

    PRD 0009. This is the deterministic replacement for the old prose recipe
    that asked the model to hand-build ``{"component":"Mail", props}`` — the
    exact step a weak local model failed to perform (2026-05-30 RC1). The card
    is now built here, from the data the search already returned:

    - **digest** (→ transcript): ``count`` + ``query`` + a body-free, capped
      list of ``{subject, from, receivedAt}`` — no ``bodyPreview`` (0056) and
      a fraction of the full blob's size (PRD 0009 context saving);
    - **deliverable** (→ overlay): **one ``{"component":"Mail", "props": msg}``
      section per returned message, in result order** (each ``msg`` already
      matches the ``Mail`` props schema via ``to_mail_props``), else ``None``
      when no usable message is present (PRD 0010 / issue 0067 — every matched
      mail surfaces as its own card so "3 derniers mails" renders three cards,
      fixing the mono-card bug; only ``dict`` messages contribute a section, so
      a stray non-dict in the list is skipped rather than crashing);
    - **summary** (→ spoken ``result_summary``): a deterministic French line;
    - **terminal**: always ``True`` — a mail lookup is single-shot, so the
      runner may converge on the first result (empty or not) instead of waiting
      for the model to emit ``done`` (2026-05-30 fix #2).
    """

    count = int(result.get("count") or 0)
    messages = result.get("messages")
    messages = messages if isinstance(messages, list) else []

    digest_messages: list[dict[str, Any]] = []
    for msg in messages[:_GMAIL_DIGEST_MAX_MESSAGES]:
        if not isinstance(msg, dict):
            continue
        entry: dict[str, Any] = {key: msg.get(key) for key in _GMAIL_DIGEST_MESSAGE_FIELDS}
        sender = msg.get("from")
        entry["from"] = sender.get("name") if isinstance(sender, dict) else None
        digest_messages.append(entry)
    digest: dict[str, Any] = {
        "count": count,
        "query": result.get("query"),
        "messages": digest_messages,
    }

    # One Mail section per usable message, preserving result order (issue 0067).
    # Non-dict entries are skipped — a malformed item must never crash the
    # projection nor poison the section list.
    usable: list[dict[str, Any]] = [msg for msg in messages if isinstance(msg, dict)]
    sections: list[dict[str, Any]] = [{"component": "Mail", "props": msg} for msg in usable]

    if count > 0 and usable:
        first = usable[0]
        subject = first.get("subject") or "(sans objet)"
        sender = first.get("from")
        sender_name = sender.get("name") if isinstance(sender, dict) else None
        # The spoken summary is unchanged from issue 0066: count + the first
        # ("Dernier") message's subject/sender. It deliberately does NOT
        # enumerate every mail — the cards carry the per-message detail.
        summary = (
            f"{count} email(s) trouvé(s). Dernier : « {subject} »"
            + (f" de {sender_name}" if sender_name else "")
            + "."
        )
        return ProjectedResult(
            digest=digest,
            deliverable=sections,
            summary=summary,
            terminal=True,
        )

    return ProjectedResult(
        digest=digest,
        deliverable=None,
        summary="Aucun email ne correspond à cette recherche.",
        terminal=True,
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
            "mail précis. ``max_results`` n'a pas de limite haute (1 par "
            "défaut) ; chaque message renvoyé donne une carte ``Mail``."
        ),
        args_model=GmailSearchArgs,
        handler=_gmail_search_handler,
        # PRD 0009 — the runner builds the Mail card + spoken summary from this
        # projection deterministically; the model no longer hand-builds it.
        result_projector=project_gmail_search,
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
    "project_gmail_search",
]
