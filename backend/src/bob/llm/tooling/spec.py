"""Canonical :class:`ToolSpec` — the single source of truth for a tool's wire shape.

PRD 0008 (tool-calling unification) collapses the two divergent tool
descriptors Bob carries today onto one canonical shape:

- Jarvis side: :class:`bob.tools.registry.ToolDefinition` holds a hand-written
  ``parameters`` JSON Schema dict and projects to
  :class:`bob.llm.types.ToolDefinition` for the wire.
- Sub-agent side: :class:`bob.sub_agent.tool_registry.SubAgentToolDefinition`
  holds a Pydantic ``args_model`` and validates against it directly.

A :class:`ToolSpec` unifies both: ``parameters`` is *derived* from the Pydantic
``args_model`` via :meth:`pydantic.BaseModel.model_json_schema` when one is
available, or wraps an already-built JSON Schema dict (the Jarvis path, which
keeps its hand-written schema for now). Either way call sites see the same
``(name, description, parameters)`` triple — the codec layer
(:mod:`bob.llm.tooling.codec`) consumes only that triple, so it never has to
know which registry the spec came from.

This is issue 0058 (P1). The two registries stay separate (they have different
visibility — a sub-agent must never see ``say`` / ``spawn_task``); they just
both become *derivable* to a ``ToolSpec``. Routing the sub-agent registry
through :meth:`from_args_model` is issue 0059's job — here we only define the
shape and the constructors. Schema *flattening* (collapsing ``$defs`` /
``$ref`` for picky local models) is issue 0063 — here we derive the schema
verbatim from Pydantic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bob.llm.tooling.schema import flatten_schema
from bob.llm.types import ToolDefinition

if TYPE_CHECKING:  # pragma: no cover — typing-only import.
    from pydantic import BaseModel


@dataclass(frozen=True)
class ToolSpec:
    """Backend-agnostic description of one callable tool.

    ``parameters`` is a JSON Schema object (the same shape OpenAI uses for the
    ``function.parameters`` field). ``args_model``, when present, is the
    Pydantic model the schema was derived from — kept on the spec so a later
    phase (self-correction, issue 0062) can re-validate parsed arguments
    without re-plumbing the model through the call stack. A spec built from a
    raw :class:`bob.llm.types.ToolDefinition` (the Jarvis path today) carries
    ``args_model=None``.

    Frozen + slotted-ish (frozen dataclass) so a spec can be shared across
    codec instances without any aliasing risk, mirroring the repo's existing
    :class:`bob.llm.types.ToolDefinition` style.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    args_model: type[BaseModel] | None = None

    @classmethod
    def from_args_model(
        cls,
        *,
        name: str,
        description: str,
        args_model: type[BaseModel],
    ) -> ToolSpec:
        """Build a spec whose ``parameters`` is derived from ``args_model``.

        ``parameters`` is the model's ``model_json_schema()`` run once through
        :func:`bob.llm.tooling.schema.flatten_schema` (issue 0063) — ``$ref`` /
        ``$defs`` inlined, ``Optional`` / string-enum unions collapsed to flat
        shapes, nesting capped — so the schema a picky local model or guided
        decoder receives never carries a construct it would reject. Flattening
        runs here, at the single point every sub-agent spec is derived, so the
        whole tool surface is hygienic by construction. ``args_model`` is kept
        intact for post-decode validation (the flat schema gates the wire, the
        full model gates semantics).

        This is the constructor the sub-agent registry routes through. It is
        defined here so ``ToolSpec`` is provably derivable from a Pydantic model.
        """

        return cls(
            name=name,
            description=description,
            parameters=flatten_schema(args_model.model_json_schema()),
            args_model=args_model,
        )

    @classmethod
    def from_tool_definition(cls, definition: ToolDefinition) -> ToolSpec:
        """Wrap an existing :class:`bob.llm.types.ToolDefinition` as a spec.

        The Jarvis-side registry hands the codec its already-projected
        :class:`bob.llm.types.ToolDefinition` (hand-written ``parameters``
        JSON Schema). This adapter lets the native call site speak in
        :class:`ToolSpec` without forcing the Jarvis registry to grow an
        ``args_model``-derived schema yet — behaviour-preserving for P1. The
        resulting spec carries ``args_model=None`` because the wire-level
        :class:`bob.llm.types.ToolDefinition` does not retain the model.
        """

        return cls(
            name=definition.name,
            description=definition.description,
            parameters=definition.parameters,
            args_model=None,
        )

    def to_tool_definition(self) -> ToolDefinition:
        """Project back to the wire-level :class:`bob.llm.types.ToolDefinition`.

        Lets a codec that still needs the legacy triple (and the debug-event
        plumbing keyed on it) keep working unchanged while call sites migrate
        to :class:`ToolSpec`. Pure data reshuffle — no schema transformation.
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


__all__ = ["ToolSpec"]
