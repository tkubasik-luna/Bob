"""``say`` tool definition — unified Jarvis emission (PRD 0006 / issue 0047).

Pre-0047 the orchestrator had two emission paths:

* The ``complete()`` tool-call path for ``spawn_subtask`` /
  ``forward_to_subtask`` / ``cancel_subtask``.
* A free-form ``chat()`` path with JSON-schema enforcement
  (``_reply_with_structured_response``) for everything else — plain replies.

Issue 0047 collapses the second path into a single ``say`` tool. After
this slice every Jarvis turn is exactly one dispatched tool call: ``say``
for direct replies, the task tools for everything else. A known-shape
tool-call argument string is the only thing the partial-JSON parser
will be asked to parse in 0049, and the ``jarvis.route`` structured
event introduced in 0044 now logs on every turn (including replies —
previously a blind spot).

Handler contract:

* Validate ``speech`` (non-empty after strip) + optional ``ui`` (free-form
  object). The Pydantic model enforces ``speech: str`` with
  ``min_length=1`` so empty / whitespace-only speech fails validation
  through the same code path as bad ``spawn_subtask`` args.
* Persist the assistant turn in :class:`bob.jarvis_store.JarvisStore` so
  the next user turn's context assembly sees the reply as part of
  history. This mirrors what the legacy
  ``_reply_with_structured_response`` path did at the end.
* Return :class:`ToolHandlerOutcome` with ``speech`` + ``ui`` populated
  so the orchestrator can lift them into :class:`OrchestratorResponse`
  (and ultimately the ``assistant_msg`` WS frame the frontend already
  consumes). The handler intentionally does NOT emit ``assistant_msg``
  itself — the WS router emits it once after ``process_user_message``
  returns. Emitting from the handler would double-fire the frame and
  trigger double-TTS in voice mode.

The 0048 retry/degrade slice will wire validation failures here through
the same dispatcher path: a hardcoded fallback ``say("Désolé, peux-tu
reformuler ?")`` is dispatched when the LLM exhausts its retry budget.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome
from bob.ui_registry import get_say_ui_schema

_logger = structlog.get_logger(__name__)


class SayArgs(BaseModel):
    """Validated argument shape for the ``say`` tool.

    ``speech`` is required and non-empty after strip (enforced by the
    Pydantic ``min_length=1`` constraint at the JSON-schema layer; the
    handler strips and re-checks so leading whitespace cannot smuggle a
    blank reply through).

    ``ui`` is optional and free-form: the LLM may emit ``null`` (a plain
    spoken reply with no visual payload) or an object describing a UI
    component (``{"component": "Markdown", "props": {...}}``). The
    orchestrator normalises the value into the existing
    :class:`bob.ui_registry.ComponentDescriptor` list shape. Multiple
    components (the legacy structured-output path supported a list)
    remain out of scope for the v1 ``say`` tool — issue 0050 introduces
    the richer overlay surface alongside the new task tools.
    """

    # PRD 0006 lifts the structured ``ui`` payload onto a tool argument;
    # Pydantic v2 requires ``model_config`` to opt into the ``Any`` field
    # so the model serialises arbitrary nested objects without nagging.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    speech: str = Field(
        ...,
        min_length=1,
        description=(
            "Le texte à dire à l'utilisateur. Texte simple, en français, "
            "naturel et concis. C'est le seul champ obligatoire."
        ),
    )
    ui: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Composant UI optionnel à afficher en plus de la parole. Objet "
            "structuré au format {component, props}, ou ``null`` quand la "
            "réponse est purement vocale."
        ),
    )


_SAY_DESCRIPTION = (
    "Réponds directement à l'utilisateur. C'est l'outil par défaut pour "
    "toute interaction qui ne nécessite pas de déléguer à une sous-tâche "
    "ni de transmettre une réponse. Le champ ``speech`` est obligatoire ; "
    "le champ ``ui`` est optionnel et accepte un objet ``{component, "
    "props}`` ou ``null``."
)


_SAY_PARAMETERS = {
    "type": "object",
    "properties": {
        "speech": {
            "type": "string",
            "description": (
                "Le texte à dire à l'utilisateur. Texte simple, en français, naturel et concis."
            ),
        },
        "ui": get_say_ui_schema(),
    },
    "required": ["speech"],
}


async def _say_handler(ctx: ToolHandlerContext, args: BaseModel) -> ToolHandlerOutcome:
    """Persist the spoken reply + thread ``speech`` / ``ui`` to the orchestrator.

    The handler uses ``ctx.jarvis_store`` (the DI-injected handle owned
    by the orchestrator) to persist the assistant turn so the next user
    message's context assembly sees the reply in history. When the
    optional handle is missing (narrow registry-only test harnesses that
    never wired the store) the handler skips persistence and still
    returns the payload — the dispatcher contract is "speech in /
    structured outcome out", regardless of side effects.

    The handler intentionally does NOT emit ``assistant_msg`` via
    ``ctx.ws_emit``. The WS router emits exactly one ``assistant_msg``
    frame after :meth:`Orchestrator.process_user_message` returns; a
    second emit here would double-fire the frame and trigger double TTS
    in voice mode.
    """

    assert isinstance(args, SayArgs)
    speech = args.speech.strip()
    if not speech:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="speech is empty after strip",
        )

    if ctx.jarvis_store is not None:
        try:
            ctx.jarvis_store.append("assistant", speech)
        except Exception:  # pragma: no cover — defensive net.
            _logger.exception("orchestrator.say_persist_failed")

    _logger.info(
        "orchestrator.say",
        speech_chars=len(speech),
        has_ui=args.ui is not None,
    )
    return ToolHandlerOutcome(
        status="ok",
        speech=speech,
        ui=args.ui,
    )


def build_say_tool() -> ToolDefinition:
    """Construct the registry entry for ``say`` (v1)."""

    return ToolDefinition(
        name="say",
        version="v1",
        description=_SAY_DESCRIPTION,
        parameters=_SAY_PARAMETERS,
        args_model=SayArgs,
        handler=_say_handler,
    )
