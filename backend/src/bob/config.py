"""Application configuration loaded from environment / `.env`.

Two LLM backends are supported, selected by ``LLM_PROVIDER``:

- ``lm_studio`` (default): OpenAI-compatible HTTP endpoint. Requires
  ``LLM_BASE_URL``, ``LLM_MODEL``, ``LLM_API_KEY``.
- ``claude_cli``: subprocess call to the ``claude`` CLI in ``-p`` mode.
  Requires ``claude`` on ``PATH`` (or set ``CLAUDE_CLI_BIN``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Immutable application settings.

    Loaded from environment variables, optionally backed by a ``.env`` file
    located next to the process working directory.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        frozen=True,
        extra="ignore",
    )

    # LLM provider selection
    LLM_PROVIDER: Literal["lm_studio", "claude_cli"] = "lm_studio"

    # LM Studio / OpenAI-compatible backend (required when LLM_PROVIDER=lm_studio)
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str | None = None
    LLM_API_KEY: str | None = None
    LLM_TIMEOUT_SECONDS: float = 60.0

    # Claude CLI backend (used when LLM_PROVIDER=claude_cli)
    CLAUDE_CLI_BIN: str = "claude"
    CLAUDE_CLI_MODEL: str | None = None
    CLAUDE_CLI_TIMEOUT_SECONDS: float = 120.0

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> Settings:
        if self.LLM_PROVIDER == "lm_studio":
            missing = [
                name
                for name, value in (
                    ("LLM_BASE_URL", self.LLM_BASE_URL),
                    ("LLM_MODEL", self.LLM_MODEL),
                    ("LLM_API_KEY", self.LLM_API_KEY),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"LLM_PROVIDER=lm_studio requires: {', '.join(missing)}"
                )
        return self

    # Backend
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8000

    # Logging
    LOG_LEVEL: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Raises :class:`pydantic.ValidationError` at first call if a required
    variable is missing — crashing the process early as designed.
    """

    return Settings()
