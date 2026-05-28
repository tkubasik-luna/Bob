"""Versioned :class:`ToolDefinition` + :class:`ToolRegistry`.

Each :class:`ToolDefinition` carries:

- ``name`` — the public tool name surfaced to the LLM (``"spawn_subtask"``,
  ``"forward_to_subtask"``, ``"cancel_subtask"``). The LLM only sees
  ``name`` in the tool list; ``version`` is internal bookkeeping.
- ``version`` — semantic-ish identifier (``"v1"``, ``"v2"``) so swapping
  the local LLM model never silently breaks Jarvis (PRD 0006 user story
  #20). Combined with ``name`` it gives the canonical
  ``"v1.spawn_subtask"`` form used in ``jarvis.route`` events.
- ``description`` / ``parameters`` — JSON-Schema description sent to the
  LLM (same shape OpenAI uses). Mirrors today's
  :class:`bob.llm.types.ToolDefinition` so the registry can be projected
  into the legacy structure with :meth:`ToolDefinition.to_llm_definition`.
- ``args_model`` — Pydantic v2 model class validated by the dispatcher
  before invoking ``handler``. The model is the single source of truth
  for arg shape; ``parameters`` should be kept in sync (the contract test
  in ``tests/test_tool_registry.py`` enforces required-field parity).
- ``handler`` — async callable ``(ctx, args) -> ToolHandlerOutcome`` that
  performs the side effect (creating a task row, resuming a sub-agent…)
  and returns an outcome describing what happened.

The registry is built once at orchestrator construction time via
:func:`build_default_registry`. Tests can construct their own registry
with a narrower set of tools when they exercise the dispatcher in
isolation.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bob.llm.types import ToolDefinition as LLMToolDefinition
from bob.tools.types import ToolHandler

if TYPE_CHECKING:  # pragma: no cover — typing-only.
    from pydantic import BaseModel


class ToolArgsValidationError(ValueError):
    """Raised when a tool call's arguments fail Pydantic validation.

    The dispatcher catches this and surfaces it as a structured
    :class:`bob.tools.dispatcher.DispatchResult` so the orchestrator can
    react to it. We keep a dedicated exception type (rather than reusing
    :class:`pydantic.ValidationError`) so call sites can pattern-match on
    the registry's contract instead of pinning to the Pydantic version.
    """

    def __init__(self, *, tool_name: str, message: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.message = message


@dataclass(frozen=True)
class ToolDefinition:
    """A versioned tool the dispatcher can execute.

    The combination ``(name, version)`` is what we log in ``jarvis.route``
    events (PRD 0006 user story #19): one stable identifier per concrete
    behavior. Today every tool is at ``version="v1"``; later slices may
    introduce ``v2`` variants (e.g. ``spawn_subtask`` with cost reporting)
    without breaking the LLM-facing ``name``.
    """

    name: str
    version: str
    description: str
    parameters: dict[str, Any]
    args_model: type[BaseModel]
    handler: ToolHandler

    @property
    def qualified_name(self) -> str:
        """Return the canonical ``"v1.spawn_subtask"`` identifier.

        Used in ``jarvis.route`` events + audit logs. ``name`` stays the
        unversioned LLM-facing label.
        """

        return f"{self.version}.{self.name}"

    def to_llm_definition(self) -> LLMToolDefinition:
        """Project this entry to the existing :class:`LLMToolDefinition`.

        Lets the orchestrator keep handing the same shape to
        :meth:`bob.llm_client.LLMClient.complete` without changing the LLM
        contract. The registry remains the source of truth.
        """

        return LLMToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


class ToolRegistry:
    """Ordered, name-indexed collection of :class:`ToolDefinition`.

    Order matters because the registry projects itself to the LLM-facing
    tool list (:meth:`as_llm_definitions`) in registration order — the
    LLM prompt addendum in :mod:`bob.orchestrator` describes the tools in
    the same order, so we keep them aligned.
    """

    def __init__(self, definitions: list[ToolDefinition] | None = None) -> None:
        self._definitions: list[ToolDefinition] = []
        self._by_name: dict[str, ToolDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        """Add ``definition`` to the registry.

        Raises :class:`ValueError` when a tool with the same ``name`` is
        already registered. We disallow re-registration so a typo or a
        merge accident surfaces loudly at boot rather than silently
        shadowing the previous entry.
        """

        if definition.name in self._by_name:
            raise ValueError(f"tool already registered: {definition.name}")
        self._definitions.append(definition)
        self._by_name[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        """Return the definition for ``name`` (or ``None`` for unknown tools).

        Unknown-tool handling is the dispatcher's job (it turns the
        lookup miss into a :class:`DispatchResult` and emits a
        ``jarvis.route`` event). The registry stays a dumb lookup.
        """

        return self._by_name.get(name)

    def names(self) -> list[str]:
        """Return registered tool names in insertion order."""

        return [d.name for d in self._definitions]

    def as_llm_definitions(self) -> list[LLMToolDefinition]:
        """Project the registry to the LLM-facing tool list.

        Order matches registration order so the addendum strings in
        :mod:`bob.orchestrator` describe the tools in the same sequence
        the LLM sees them.
        """

        return [d.to_llm_definition() for d in self._definitions]

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self._definitions)

    def __len__(self) -> int:
        return len(self._definitions)


def build_default_registry() -> ToolRegistry:
    """Construct the default Jarvis-side tool registry.

    Registered tools (in LLM-facing display order):

    1. ``say`` (issue 0047) — unified direct-reply path.
    2. ``show_task_result`` — recall a stored task deliverable.
    3. ``spawn_task`` (issue 0050) — v2 spawn entry point.
    4. ``addendum_task`` (issue 0050) — push info into a running runner.
    5. ``replan_task`` (issue 0050) — cancel + respawn with lineage.
    6. ``cancel_task`` (issue 0050) — v2 cancel entry point.

    The v1 aliases ``spawn_subtask`` / ``forward_to_subtask`` /
    ``cancel_subtask`` (issue 0044) have been removed: every call site
    has migrated to the v2 task surface, and the prompt no longer
    advertises them.

    The order pins the LLM-facing tool list. The ``TOOLS_SYSTEM_ADDENDUM``
    prompt fragment must describe the tools in this exact order so the
    LLM picks the right tool for the right intent.
    """

    # Imported lazily so ``bob.tools`` does not eagerly drag the
    # orchestrator-adjacent modules into the import graph during the
    # tests that target the registry in isolation.
    from bob.tools.definitions.addendum_task import build_addendum_task_tool
    from bob.tools.definitions.cancel_task import build_cancel_task_tool
    from bob.tools.definitions.replan_task import build_replan_task_tool
    from bob.tools.definitions.say import build_say_tool
    from bob.tools.definitions.show_task_result import build_show_task_result_tool
    from bob.tools.definitions.spawn_task import build_spawn_task_tool

    return ToolRegistry(
        [
            build_say_tool(),
            build_show_task_result_tool(),
            build_spawn_task_tool(),
            build_addendum_task_tool(),
            build_replan_task_tool(),
            build_cancel_task_tool(),
        ]
    )
