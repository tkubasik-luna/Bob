"""Tests for :mod:`bob.config`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bob.config import Settings


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS",
        "BACKEND_HOST",
        "BACKEND_PORT",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-7b-instruct")
    monkeypatch.setenv("LLM_API_KEY", "lm-studio")

    settings = Settings()  # type: ignore[call-arg]

    assert settings.LLM_BASE_URL == "http://localhost:1234/v1"
    assert settings.LLM_MODEL == "qwen2.5-7b-instruct"
    assert settings.LLM_API_KEY == "lm-studio"
    assert settings.LLM_TIMEOUT_SECONDS == 60.0
    assert settings.BACKEND_HOST == "127.0.0.1"
    assert settings.BACKEND_PORT == 8000
    assert settings.LOG_LEVEL == "INFO"


def test_settings_crashes_when_required_var_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    # Only set two of the three required vars
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-7b-instruct")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_is_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-7b-instruct")
    monkeypatch.setenv("LLM_API_KEY", "lm-studio")

    settings = Settings()  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        settings.LLM_MODEL = "other"
