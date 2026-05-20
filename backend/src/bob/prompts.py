"""Prompt template loader.

Loads every ``*.md`` file under ``backend/prompts/`` at import time and exposes
:func:`render` to interpolate ``str.format``-style placeholders.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompts(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {path.stem: path.read_text(encoding="utf-8") for path in directory.glob("*.md")}


_PROMPTS: dict[str, str] = _load_prompts(_PROMPTS_DIR)


def render(name: str, **kwargs: object) -> str:
    """Render the prompt ``name`` with ``kwargs`` as ``str.format`` arguments.

    Raises :class:`KeyError` if ``name`` is unknown.
    """

    try:
        template = _PROMPTS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown prompt: {name!r}") from exc
    return template.format(**kwargs)


def available() -> list[str]:
    """Return the sorted list of loaded prompt names."""

    return sorted(_PROMPTS)
