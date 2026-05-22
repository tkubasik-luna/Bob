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

    # Orchestrator backends — slice #0018.
    # When unset they fall back to ``LLM_PROVIDER`` so callers can route the
    # Jarvis role and the sub-agent role to different backends if they want
    # (e.g. fast local LM Studio for Jarvis, claude-cli for sub-agents) while
    # the default keeps everything on a single backend.
    JARVIS_BACKEND: str | None = None
    SUBAGENT_BACKEND: str | None = None

    # Implicit cap on concurrent running sub-tasks. The real cap + queue land
    # in slice #0020; this field exists now so callers can reference it
    # without breaking the config contract when the cap is wired up.
    MAX_RUNNING_TASKS: int = 3

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
                raise ValueError(f"LLM_PROVIDER=lm_studio requires: {', '.join(missing)}")
        return self

    # Backend
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8000

    # Logging
    LOG_LEVEL: str = "INFO"

    # Persistence — Jarvis thread + future task data live under this directory.
    # Resolved lazily so tests can override via ``BOB_DATA_DIR`` env var with a
    # tmp path; the boot path in :mod:`bob.main` ensures the directory exists.
    BOB_DATA_DIR: Path = Path.home() / ".bob"

    # Kokoro TTS — local engine via the upstream ``kokoro`` (KPipeline) package.
    # Model weights are downloaded by Hugging Face's cache the first time the
    # pipeline is instantiated (``hexgrad/Kokoro-82M``); no manual artifacts.
    # ``KOKORO_LANG_CODE`` is the single-letter pipeline language ('f' = French,
    # 'a' = American English, 'b' = British English, etc.) used by KPipeline +
    # misaki G2P. Sample rate is a model constant exposed by
    # :data:`bob.tts_service.KOKORO_SAMPLE_RATE` — not a settings dial.
    KOKORO_LANG_CODE: str = "f"
    KOKORO_DEFAULT_VOICE: str = "ff_siwis"
    KOKORO_DEFAULT_SPEED: float = 1.0
    KOKORO_HF_REPO_ID: str = "hexgrad/Kokoro-82M"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Raises :class:`pydantic.ValidationError` at first call if a required
    variable is missing — crashing the process early as designed.
    """

    return Settings()
