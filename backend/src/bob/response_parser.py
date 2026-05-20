"""Parse and validate raw LLM output, with a single self-correcting retry.

Happy path: ``json.loads`` + :func:`bob.ui_registry.validate_response`.
On failure (JSON decode error or schema violation), we ask the LLM to retry
once with a correction message; if that still fails, we fall back to a
plain-text-only :class:`ParsedResponse`.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from bob import ui_registry
from bob.llm_client import LLMClient
from bob.ui_registry import ParsedResponse, ResponseSchemaError

_logger = structlog.get_logger(__name__)


def _try_parse(raw: str) -> tuple[ParsedResponse | None, str | None]:
    """Attempt to decode + validate ``raw``.

    Returns ``(parsed, None)`` on success, or ``(None, error_message)`` on failure.
    """

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
    if not isinstance(payload, dict):
        return None, "top-level JSON value must be an object"
    try:
        return ui_registry.validate_response(payload), None
    except ResponseSchemaError as exc:
        return None, str(exc)


async def parse(
    raw_llm_output: str,
    llm_client: LLMClient,
    messages_so_far: list[dict[str, Any]],
) -> ParsedResponse:
    """Parse ``raw_llm_output``; retry once via ``llm_client`` on failure.

    If both attempts fail, returns ``ParsedResponse(speech=raw_first_attempt, ui=[])``
    so the caller can still surface something to the user.
    """

    parsed, error = _try_parse(raw_llm_output)
    if parsed is not None:
        return parsed

    _logger.warning(
        "response_parser.first_attempt_failed",
        error=error,
        raw_preview=raw_llm_output[:200],
    )

    correction = (
        f"Ton dernier message était invalide : {error}. "
        "Réessaye en respectant strictement le schéma."
    )
    retry_messages: list[dict[str, Any]] = [
        *messages_so_far,
        {"role": "assistant", "content": raw_llm_output},
        {"role": "user", "content": correction},
    ]

    retry_raw = await llm_client.chat(retry_messages, schema=ui_registry.get_response_schema())
    retry_parsed, retry_error = _try_parse(retry_raw)
    if retry_parsed is not None:
        return retry_parsed

    _logger.warning(
        "response_parser.retry_failed",
        error=retry_error,
        raw_preview=retry_raw[:200],
    )
    return ParsedResponse(speech=raw_llm_output, ui=[])
