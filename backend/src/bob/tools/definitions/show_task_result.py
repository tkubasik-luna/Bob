"""``show_task_result`` tool — surface a stored task deliverable as a UI card.

Background: when the user revisits a topic Jarvis has already delegated to a
sub-task (« ressort les infos sur le pizza day »), the pre-existing path was
to call ``say`` and inline-regenerate the full Markdown deliverable inside the
``ui`` argument. That re-paid the generation cost on every recall, blew up
the LLM-emitted token count, and was the exact place where the
``{component, content}`` shape bug hid (fixed in the say-shape coercion).

This tool removes the regeneration: Jarvis emits a short introduction phrase
in ``speech`` plus a free-text ``query`` describing the task to surface. The
backend fuzzy-matches against :meth:`bob.task_store.TaskStore.find_by_query`,
pulls the stored ``task.result`` (the canonical Markdown the sub-agent
produced via ``done.ui_payload``), and returns it through the standard
``ToolHandlerOutcome`` ``ui`` field. The orchestrator then lifts speech + ui
into the live ``ui_payload`` + final ``assistant_msg`` frames using the same
plumbing as ``say`` (see ``Orchestrator._dispatch_tool_calls``).

Failure modes are explicit so the validation/retry layer can react:

* ``no_matching_task`` — fuzzy match returned zero rows. Jarvis retries (and
  on degrade falls back to the hardcoded "Désolé, peux-tu reformuler ?").
* ``no_persisted_result`` — match found but the task row carries no
  ``result`` yet (e.g. a sub-task still running). Jarvis retries.

Both retries leave the door open for the LLM to either reword the query or
fall back to ``say`` on its next attempt.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from bob.tools.dispatcher import ToolHandlerContext
from bob.tools.registry import ToolDefinition
from bob.tools.types import ToolHandlerOutcome

_logger = structlog.get_logger(__name__)


class ShowTaskResultArgs(BaseModel):
    """Validated argument shape for the ``show_task_result`` tool."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    speech: str = Field(
        ...,
        min_length=1,
        description=(
            "Phrase d'introduction courte que Jarvis dit avant l'affichage. "
            "Ex : « Tu m'avais demandé un focus sur le pizza day, voilà : »."
        ),
    )
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Texte court décrivant la tâche à retrouver (titre, sujet, "
            "mots-clés). Le backend fait une recherche floue sur le titre "
            "et l'objectif des tâches stockées."
        ),
    )


_DESCRIPTION = (
    "Affiche à l'écran le livrable d'une tâche déjà terminée et stockée. "
    "Utilise ce tool quand l'utilisateur veut revoir ou être ré-informé sur "
    "un sujet qu'une sous-tâche a déjà traité (« ressors X », « rappelle-moi "
    "ce que tu avais trouvé sur Y »). Tu N'AS PAS à re-générer le contenu : "
    "le backend retrouve la tâche via une recherche floue sur son "
    "titre/objectif et envoie le livrable Markdown stocké tel quel. Fournis "
    "juste une courte phrase d'introduction (``speech``) et la requête de "
    "recherche (``query``)."
)


_PARAMETERS = {
    "type": "object",
    "properties": {
        "speech": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Phrase d'introduction courte (1 phrase max) — ce que Jarvis "
                "dit avant que le livrable apparaisse à l'écran."
            ),
        },
        "query": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Mots-clés / titre approximatif de la tâche à retrouver. Ex : "
                '"pizza day", "exposé révolution française".'
            ),
        },
    },
    "required": ["speech", "query"],
}


async def _show_task_result_handler(
    ctx: ToolHandlerContext, args: BaseModel
) -> ToolHandlerOutcome:
    """Look up the stored deliverable and thread speech + ui to the orchestrator.

    Persistence mirrors ``say``: the spoken intro is appended to the
    :class:`bob.jarvis_store.JarvisStore` so the next user turn's context
    sees the assistant reply in history. The handler does NOT emit
    ``assistant_msg`` itself — the WS router emits the final frame once
    after :meth:`Orchestrator.process_user_message` returns.
    """

    assert isinstance(args, ShowTaskResultArgs)
    speech = args.speech.strip()
    query = args.query.strip()
    if not speech:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="speech is empty after strip",
        )
    if not query:
        return ToolHandlerOutcome(
            status="error",
            error_code="invalid_args",
            error_message="query is empty after strip",
        )

    # Prefer ``done`` tasks first — that's the natural target for a
    # "ressort les infos" intent. A still-running task with a partial
    # progress write would be misleading to surface.
    matches = ctx.task_store.find_by_query(
        query, prefer_state="done", limit=1
    )
    if not matches:
        _logger.info(
            "orchestrator.show_task_result_no_match",
            query=query,
        )
        return ToolHandlerOutcome(
            status="error",
            error_code="no_matching_task",
            error_message=f"no task matches query={query!r}",
        )

    task = matches[0]
    if not task.result or not task.result.strip():
        _logger.info(
            "orchestrator.show_task_result_no_persisted_result",
            query=query,
            task_id=task.id,
            task_state=task.state,
        )
        return ToolHandlerOutcome(
            status="error",
            error_code="no_persisted_result",
            error_message=(
                f"task {task.id} ({task.state}) matches query but has no "
                "persisted result yet"
            ),
        )

    ui: dict[str, Any] = {
        "component": "Markdown",
        "props": {"content": task.result},
    }

    if ctx.jarvis_store is not None:
        try:
            ctx.jarvis_store.append("assistant", speech)
        except Exception:  # pragma: no cover — defensive net.
            _logger.exception("orchestrator.show_task_result_persist_failed")

    _logger.info(
        "orchestrator.show_task_result",
        query=query,
        task_id=task.id,
        task_state=task.state,
        speech_chars=len(speech),
        result_chars=len(task.result),
    )
    return ToolHandlerOutcome(
        status="ok",
        speech=speech,
        ui=ui,
    )


def build_show_task_result_tool() -> ToolDefinition:
    """Construct the registry entry for ``show_task_result`` (v1)."""

    return ToolDefinition(
        name="show_task_result",
        version="v1",
        description=_DESCRIPTION,
        parameters=_PARAMETERS,
        args_model=ShowTaskResultArgs,
        handler=_show_task_result_handler,
    )
