"""UI component registry — single source of truth for the LLM → UI contract.

Defines the catalogue of UI components the LLM is allowed to emit, the JSON
Schema for the full response payload (``{speech, ui}``) and validation
helpers. The schema is shaped so it can be passed straight to LM Studio's
``response_format=json_schema`` via :class:`bob.llm_client.LMStudioClient`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, Field


class ComponentDescriptor(BaseModel):
    """A single UI component instance emitted by the LLM."""

    component: str
    props: dict[str, Any] = Field(default_factory=dict)


class ParsedResponse(BaseModel):
    """A validated LLM response: spoken text + a list of UI components."""

    speech: str
    ui: list[ComponentDescriptor] = Field(default_factory=list)


class ResponseSchemaError(Exception):
    """Raised when an LLM payload fails JSON-Schema validation."""

    def __init__(self, message: str, errors: list[str]) -> None:
        super().__init__(message)
        self.errors = errors


@dataclass(frozen=True)
class UIComponent:
    """Definition of a UI component available to the LLM."""

    name: str
    props_schema: dict[str, Any]
    description: str = ""


@dataclass(frozen=True)
class UIRegistry:
    """A registry of available UI components."""

    components: dict[str, UIComponent] = field(default_factory=dict)

    def component_schemas(self) -> list[dict[str, Any]]:
        """Return the per-component JSON sub-schemas used in the top-level ``oneOf``."""

        schemas: list[dict[str, Any]] = []
        for comp in self.components.values():
            schemas.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["component", "props"],
                    "properties": {
                        "component": {"type": "string", "const": comp.name},
                        "props": comp.props_schema,
                    },
                }
            )
        return schemas

    def response_schema(self) -> dict[str, Any]:
        """Build the JSON Schema for the full LLM response.

        Returns the LM Studio-compatible wrapper
        ``{"name": ..., "schema": {...}, "strict": true}`` so it can be passed
        directly to :meth:`bob.llm_client.LLMClient.chat` as ``schema=``.
        """

        inner: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["speech", "ui"],
            "properties": {
                "speech": {"type": "string"},
                "ui": {
                    "type": "array",
                    "items": {"oneOf": self.component_schemas()},
                },
            },
        }
        return {"name": "BobResponse", "schema": inner, "strict": True}

    def components_description_for_prompt(self) -> str:
        """Render a human-readable Markdown description of available components."""

        lines: list[str] = ["Available UI components:"]
        for comp in self.components.values():
            lines.append(f"- **{comp.name}**")
            if comp.description:
                lines.append(f"  - {comp.description}")
            props = comp.props_schema.get("properties", {})
            required = set(comp.props_schema.get("required", []))
            for prop_name, prop_schema in props.items():
                req_marker = " (required)" if prop_name in required else ""
                type_repr = _describe_prop_type(prop_schema)
                lines.append(f"  - prop `{prop_name}`: {type_repr}{req_marker}")
        return "\n".join(lines)

    def validate_response(self, payload: dict[str, Any]) -> ParsedResponse:
        """Validate ``payload`` against the response schema.

        Raises :class:`ResponseSchemaError` on failure.
        """

        inner_schema = self.response_schema()["schema"]
        validator = Draft202012Validator(inner_schema)
        errors: list[ValidationError] = sorted(
            validator.iter_errors(payload), key=lambda e: list(e.path)
        )
        if errors:
            messages = [
                f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in errors
            ]
            raise ResponseSchemaError(
                f"Response payload failed schema validation: {messages}", messages
            )
        ui_items = [
            ComponentDescriptor(component=item["component"], props=item["props"])
            for item in payload["ui"]
        ]
        return ParsedResponse(speech=payload["speech"], ui=ui_items)


def _describe_prop_type(prop_schema: dict[str, Any]) -> str:
    if "enum" in prop_schema:
        return "enum(" + ", ".join(repr(v) for v in prop_schema["enum"]) + ")"
    type_value = prop_schema.get("type", "any")
    if isinstance(type_value, list):
        return " | ".join(type_value)
    return str(type_value)


# --- V0 component definitions -------------------------------------------------

CHAT_MESSAGE = UIComponent(
    name="ChatMessage",
    description="A chat bubble attributed to either the user or the assistant.",
    props_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["role", "content"],
        "properties": {
            "role": {"type": "string", "enum": ["assistant", "user"]},
            "content": {"type": "string"},
        },
    },
)

MARKDOWN = UIComponent(
    name="Markdown",
    description="A block of Markdown-formatted text rendered as rich content.",
    props_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["content"],
        "properties": {
            "content": {"type": "string"},
        },
    },
)


def build_registry(extra_components: dict[str, UIComponent] | None = None) -> UIRegistry:
    """Construct a :class:`UIRegistry` containing the V0 components.

    ``extra_components`` is a test seam allowing callers to inject additional
    components without mutating the default registry.
    """

    components: dict[str, UIComponent] = {
        CHAT_MESSAGE.name: CHAT_MESSAGE,
        MARKDOWN.name: MARKDOWN,
    }
    if extra_components:
        components.update(extra_components)
    return UIRegistry(components=components)


_DEFAULT_REGISTRY = build_registry()


def get_response_schema() -> dict[str, Any]:
    """Return the JSON Schema (LM Studio-wrapped) for the default registry."""

    return _DEFAULT_REGISTRY.response_schema()


def get_components_description_for_prompt() -> str:
    """Return a Markdown description of the default registry's components."""

    return _DEFAULT_REGISTRY.components_description_for_prompt()


def validate_response(payload: dict[str, Any]) -> ParsedResponse:
    """Validate ``payload`` using the default registry."""

    return _DEFAULT_REGISTRY.validate_response(payload)
