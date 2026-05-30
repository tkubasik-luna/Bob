"""UI component registry — single source of truth for the LLM → UI contract.

Defines the catalogue of UI components the LLM is allowed to emit, the JSON
Schema for the full response payload (``{speech, ui}``) and validation
helpers. The schema is shaped so it can be passed straight to LM Studio's
``response_format=json_schema`` via :class:`bob.llm_client.LMStudioClient`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
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

    def _component_schema(self, comp: UIComponent) -> dict[str, Any]:
        """The canonical ``{component, props}`` sub-schema for one component.

        Single builder reused by both :meth:`component_schemas` (the ``say``
        tool's ``oneOf`` and the full ``{speech, ui}`` response) and
        :meth:`validate_component_descriptor` (the sub-agent deliverable, issue
        0065) — so the two surfaces can never drift to two Mail schemas.
        """

        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["component", "props"],
            "properties": {
                "component": {"type": "string", "const": comp.name},
                "props": comp.props_schema,
            },
        }

    def component_schemas(self) -> list[dict[str, Any]]:
        """Return the per-component JSON sub-schemas used in the top-level ``oneOf``."""

        return [self._component_schema(comp) for comp in self.components.values()]

    def validate_component_descriptor(self, payload: Any) -> list[str]:
        """Validate ONE ``{component, props}`` descriptor against the registry.

        PRD 0008 / issue 0065. A sub-agent ``done.ui_payload`` may carry a
        structured deliverable (e.g. a ``Mail`` overlay card). It must be
        validated against the SAME per-component schema the ``say`` tool uses
        (:meth:`_component_schema`) — never a second hand-written Mail schema —
        so the two contracts cannot diverge. Returns a list of human-readable
        error strings (empty == valid) so the runner can fold them into the
        ``system_validator`` self-correction feedback.

        ``format`` keywords (the ``email`` checker on ``Mail.from.email``) are
        upgraded to hard constraints via :class:`FormatChecker`, matching
        :meth:`validate_response`.
        """

        if not isinstance(payload, dict):
            return [f"<root>: descriptor must be an object, got {type(payload).__name__}"]
        name = payload.get("component")
        comp = self.components.get(name) if isinstance(name, str) else None
        if comp is None:
            known = ", ".join(sorted(self.components)) or "<none>"
            return [f"component: unknown component {name!r} (known: {known})"]
        validator = Draft202012Validator(
            self._component_schema(comp), format_checker=FormatChecker()
        )
        errors: list[ValidationError] = sorted(
            validator.iter_errors(payload), key=lambda e: list(e.path)
        )
        return [
            f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in errors
        ]

    def validate_sections(
        self, sections: list[Any] | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Validate a list of section descriptors SECTION BY SECTION (issue 0068).

        PRD 0010 robustness invariant: a single malformed section must NEVER
        blank the whole view. Each section is validated against the SAME
        per-component ``oneOf`` schema as :meth:`validate_component_descriptor`
        (no new schema) — a section with an unknown component or props that fail
        the registry schema is DROPPED, valid sections are KEPT in their
        original order. The dropped sections' errors are returned so the
        self-correction loop can still surface them.

        Returns ``(kept_sections, errors)`` where ``errors`` is a flat list of
        per-section error strings using the SAME string shape as
        :meth:`validate_component_descriptor` (each prefixed with the section
        index, e.g. ``"sections[2]: component: unknown component ..."``) so the
        ``system_validator`` self-correction loop consumes them unchanged. An
        empty / ``None`` input yields ``([], [])``.
        """

        if not sections:
            return [], []
        kept: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, section in enumerate(sections):
            section_errors = self.validate_component_descriptor(section)
            if section_errors:
                errors.extend(f"sections[{index}]: {err}" for err in section_errors)
                continue
            kept.append(section)
        return kept, errors

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
        # ``format_checker`` upgrades ``format`` keywords (e.g. ``date-time``,
        # ``email``, ``uri`` used by the Mail component) from informational
        # annotations into hard validation constraints. Without it a bad
        # ``receivedAt`` string would silently pass — the issue's acceptance
        # criteria require we reject it.
        validator = Draft202012Validator(inner_schema, format_checker=FormatChecker())
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

MAIL = UIComponent(
    name="Mail",
    description=(
        "A single Gmail message rendered as an overlay card. Drives the "
        "`MailOverlay` frontend surface: avatar, from name/role/address, "
        "subject, snippet, optional flag pills and attachment chips, plus "
        "the Gmail web URL for the OPEN action."
    ),
    props_schema={
        "type": "object",
        "additionalProperties": False,
        "required": [
            "from",
            "receivedAt",
            "subject",
            "bodyPreview",
            "threadId",
            "messageId",
            "gmailWebUrl",
        ],
        "properties": {
            "from": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "email"],
                "properties": {
                    "name": {"type": "string"},
                    # `format: email` is enforced via ``FormatChecker`` in
                    # ``UIRegistry.validate_response`` (email is one of the
                    # built-in checkers jsonschema ships, no extra dep).
                    "email": {"type": "string", "format": "email"},
                    "role": {"type": "string"},
                },
            },
            # ISO 8601 timestamp. We use a regex ``pattern`` rather than
            # ``format: date-time`` because the latter requires the optional
            # ``rfc3339-validator`` extra; the regex is good enough to catch
            # garbage strings (the issue's reject-malformed-date test) and
            # is enforced by the core validator without extra dependencies.
            # Accepts e.g. ``2026-05-28T14:22:00Z`` or
            # ``2026-05-28T14:22:00.000+02:00``.
            "receivedAt": {
                "type": "string",
                "pattern": (
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
                    r"(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
                ),
            },
            "subject": {"type": "string"},
            "bodyPreview": {"type": "string"},
            "flags": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["priority", "unread", "starred"],
                },
                "default": [],
            },
            "attachments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "sizeBytes", "mime"],
                    "properties": {
                        "name": {"type": "string"},
                        "sizeBytes": {"type": "integer", "minimum": 0},
                        "mime": {"type": "string"},
                    },
                },
                "default": [],
            },
            "threadId": {"type": "string"},
            "messageId": {"type": "string"},
            # The full Gmail web URL the OPEN action will browse to. Pattern
            # asserts an http(s) prefix so a free-form string can't slip in
            # — same dep-free strategy as ``receivedAt`` above.
            "gmailWebUrl": {
                "type": "string",
                "pattern": r"^https?://.+",
            },
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
        MAIL.name: MAIL,
    }
    if extra_components:
        components.update(extra_components)
    return UIRegistry(components=components)


_DEFAULT_REGISTRY = build_registry()


def get_response_schema() -> dict[str, Any]:
    """Return the JSON Schema (LM Studio-wrapped) for the default registry."""

    return _DEFAULT_REGISTRY.response_schema()


def get_say_ui_schema() -> dict[str, Any]:
    """JSON-Schema for the ``say`` tool's ``ui`` argument.

    Constrains the LLM to emit either ``null`` (purely spoken reply) or a
    canonical ``{component, props}`` object — one of the registry's known
    components. Passed as the tool ``parameters.ui`` sub-schema so the model
    is guided away from the flat ``{component, content}`` shape that drops
    the payload through :func:`coerce_component_descriptor`. The runtime
    stays permissive (``SayArgs.ui: dict | None``); this only shapes
    generation, it is not a hard validation gate.
    """

    return {
        "anyOf": [{"type": "null"}, *_DEFAULT_REGISTRY.component_schemas()],
        "description": (
            "Composant UI optionnel à afficher en plus de la parole. `null` "
            "pour une réponse purement vocale, sinon un objet {component, "
            'props}. Exemple : {"component": "Markdown", "props": {"content": '
            '"# Titre\\n..."}}. Le contenu va TOUJOURS dans `props`.'
        ),
    }


def coerce_component_descriptor(ui: Any) -> ComponentDescriptor | None:
    """Best-effort normalise a raw ``say.ui`` value into a ComponentDescriptor.

    Tolerates the flat shape ``{component, <prop>...}`` some LLM turns emit
    instead of the canonical ``{component, props: {...}}``: any top-level key
    besides ``component`` / ``props`` is folded into ``props`` (an explicit
    ``props`` object wins on key conflict). Returns ``None`` when ``ui`` is
    not a dict or lacks a string ``component`` — callers treat that as "no
    overlay".
    """

    if not isinstance(ui, dict):
        return None
    component = ui.get("component")
    if not isinstance(component, str):
        return None
    explicit = ui.get("props")
    if not isinstance(explicit, dict):
        explicit = {}
    siblings = {k: v for k, v in ui.items() if k not in ("component", "props")}
    props = {**siblings, **explicit}
    try:
        return ComponentDescriptor(component=component, props=props)
    except Exception:
        return None


def get_components_description_for_prompt() -> str:
    """Return a Markdown description of the default registry's components."""

    return _DEFAULT_REGISTRY.components_description_for_prompt()


def validate_response(payload: dict[str, Any]) -> ParsedResponse:
    """Validate ``payload`` using the default registry."""

    return _DEFAULT_REGISTRY.validate_response(payload)


def validate_component_descriptor(payload: Any) -> list[str]:
    """Validate a single ``{component, props}`` descriptor with the default registry.

    Issue 0065 — the sub-agent deliverable validation seam. Returns a list of
    error strings (empty == valid).
    """

    return _DEFAULT_REGISTRY.validate_component_descriptor(payload)


def validate_sections(
    sections: list[Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate a list of section descriptors with the default registry (issue 0068).

    Returns ``(kept_sections, errors)`` — see
    :meth:`UIRegistry.validate_sections`. Invalid sections are dropped per
    section (never blanking the whole list); the dropped errors use the same
    string shape as :func:`validate_component_descriptor` so the runner can
    fold them into the ``system_validator`` self-correction feedback.
    """

    return _DEFAULT_REGISTRY.validate_sections(sections)
