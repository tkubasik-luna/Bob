"""Sub-agent-side :class:`ToolRegistry` + :class:`ToolDispatcher`.

The Jarvis-side registry under :mod:`bob.tools` is reused here as the
*shape* but **not** as the same instance: a sub-agent's tool surface is
strictly disjoint from Jarvis's (no ``spawn_subtask`` / ``say`` on a
sub-agent — it would let a sub-agent spawn its own children, which is
explicitly out of scope per PRD 0006 / "Out of scope").

This slice (0045) ships two placeholder tool definitions:

- ``web_search(query)`` — issues a web search. Real implementation
  intentionally a ``NotImplementedError`` for now; the focus of this
  slice is the *registry shape* (Pydantic args validation, registry
  lookup, dispatcher event emission). The actual HTTP call lands in a
  later product slice when product priorities decide on a backend.
- ``web_fetch(url)`` — fetches a URL's content. Same placeholder
  pattern.

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

from pydantic import BaseModel, Field, ValidationError


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


def build_default_subagent_registry() -> SubAgentToolRegistry:
    """Construct the default sub-agent tool registry.

    Carries ``web_search`` + ``web_fetch`` v1. Other slices may extend
    via :meth:`SubAgentToolRegistry.register` or by constructing a
    custom registry directly (tests do this).
    """

    return SubAgentToolRegistry(
        [
            build_web_search_tool(),
            build_web_fetch_tool(),
        ]
    )


__all__ = [
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
    "build_web_fetch_tool",
    "build_web_search_tool",
]
