"""Logging configuration for the backend.

Two outputs:

* The root ``bob.*`` logger emits JSON to stdout (structlog).
* A dedicated ``bob.llm_calls`` logger writes one JSON line per LLM call to
  ``logs/llm-YYYY-MM-DD.jsonl`` (one file per day, picked at log time).

Use :func:`log_llm_call` as the single entry point for the per-call file
log — it keeps the call sites tidy and the schema in one place.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from bob.config import get_settings

_configured: bool = False
_LLM_LOGGER_NAME = "bob.llm_calls"
_LOGS_DIR = Path("logs")


class _DailyJsonlHandler(logging.Handler):
    """Append one JSON line per record to ``logs/llm-YYYY-MM-DD.jsonl``.

    The target filename is recomputed at every ``emit`` so a long-running
    process naturally rolls over at midnight (UTC) without an explicit
    rotation step. Files are opened in append mode and closed immediately
    — the volume here (one line per LLM call) makes that trivial.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._directory.mkdir(exist_ok=True)
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            path = self._directory / f"llm-{day}.jsonl"
            line = self.format(record)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            self.handleError(record)


class _JsonLineFormatter(logging.Formatter):
    """Format each record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, dict):
            payload: dict[str, Any] = dict(record.msg)
        else:
            payload = {"message": record.getMessage()}
        payload.setdefault("timestamp", datetime.now(UTC).isoformat())
        payload.setdefault("level", record.levelname)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Configure structlog + the per-call LLM file logger. Idempotent."""

    global _configured
    if _configured:
        return

    settings = get_settings()
    level_name = settings.LOG_LEVEL.upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO

    # ---- structlog → stdout (JSON) -----------------------------------------
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[logging.StreamHandler()],
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ---- bob.llm_calls → logs/llm-YYYY-MM-DD.jsonl -------------------------
    _LOGS_DIR.mkdir(exist_ok=True)

    llm_logger = logging.getLogger(_LLM_LOGGER_NAME)
    llm_logger.setLevel(logging.INFO)
    llm_logger.propagate = False
    for handler in list(llm_logger.handlers):
        llm_logger.removeHandler(handler)

    file_handler = _DailyJsonlHandler(_LOGS_DIR)
    file_handler.setFormatter(_JsonLineFormatter())
    llm_logger.addHandler(file_handler)

    _configured = True


def log_llm_call(
    *,
    session_id: str | None,
    messages: list[dict[str, Any]],
    raw_response: str,
    latency_ms: float,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    """Emit one structured JSON line for a completed LLM call."""

    if not _configured:
        configure_logging()

    payload: dict[str, Any] = {
        "event": "llm_call",
        "session_id": session_id,
        "messages": messages,
        "raw_response": raw_response,
        "latency_ms": round(latency_ms, 2),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "wall_time": time.time(),
    }
    logging.getLogger(_LLM_LOGGER_NAME).info(payload)
