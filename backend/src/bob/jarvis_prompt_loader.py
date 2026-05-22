"""Load (or bootstrap) the Jarvis personality system prompt.

The Jarvis personality lives in ``{BOB_DATA_DIR}/jarvis.md`` so the user can
edit it freely without rebuilding the app. On first run the file doesn't exist
yet — :func:`load_jarvis_prompt` writes the bundled default and returns it.

Per PRD 0003: read-once at boot, no runtime hot-reload in MVP.
"""

from __future__ import annotations

from pathlib import Path

import structlog

_logger = structlog.get_logger(__name__)


DEFAULT_JARVIS_PROMPT = (
    "Tu es Jarvis, l'assistant personnel calme, précis et concis de ton utilisateur. "
    "Tu connais ton utilisateur et tu es son seul interlocuteur. "
    "Tu peux déléguer des tâches longues à des sous-agents en arrière-plan : "
    "quand l'utilisateur demande quelque chose qui demanderait du temps ou une "
    "réflexion autonome, tu lances la tâche et lui réponds tout de suite. "
    "Tu restes naturel, tu évites les formules d'introduction inutiles, et tu "
    "réponds toujours en français."
)


_JARVIS_PROMPT_FILENAME = "jarvis.md"


def load_jarvis_prompt(data_dir: Path) -> str:
    """Return the Jarvis personality prompt, writing the default if absent.

    ``data_dir`` is the resolved ``BOB_DATA_DIR`` (e.g. ``~/.bob``). The
    directory must already exist — the boot path in :mod:`bob.main` is
    responsible for creating it.

    If ``jarvis.md`` does not exist under ``data_dir``, the bundled
    :data:`DEFAULT_JARVIS_PROMPT` is written there and returned. If it
    exists, its contents are returned verbatim (no formatting / no trim).
    """

    path = data_dir / _JARVIS_PROMPT_FILENAME
    if path.exists():
        content = path.read_text(encoding="utf-8")
        _logger.info("jarvis_prompt.loaded", path=str(path), chars=len(content))
        return content

    path.write_text(DEFAULT_JARVIS_PROMPT, encoding="utf-8")
    _logger.info("jarvis_prompt.bootstrapped", path=str(path))
    return DEFAULT_JARVIS_PROMPT
