"""Application configuration loaded from environment / `.env`.

Required environment variables (boot will crash if missing):

- ``LLM_BASE_URL``
- ``LLM_MODEL``
- ``LLM_API_KEY``

The remaining settings have defaults.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Immutable application settings.

    Loaded from environment variables, optionally backed by a ``.env`` file
    located next to the process working directory.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        frozen=True,
        extra="ignore",
    )

    # LLM
    LLM_BASE_URL: str
    LLM_MODEL: str
    LLM_API_KEY: str
    LLM_TIMEOUT_SECONDS: float = 60.0

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

    return Settings()  # type: ignore[call-arg]
