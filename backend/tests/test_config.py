"""Tests for :mod:`bob.config`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bob.config import Settings


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS",
        "CLAUDE_CLI_BIN",
        "CLAUDE_CLI_MODEL",
        "CLAUDE_CLI_TIMEOUT_SECONDS",
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

    settings = Settings()

    assert settings.LLM_BASE_URL == "http://localhost:1234/v1"
    assert settings.LLM_MODEL == "qwen2.5-7b-instruct"
    assert settings.LLM_API_KEY == "lm-studio"
    assert settings.LLM_TIMEOUT_SECONDS == 3600.0
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

    settings = Settings()

    with pytest.raises(ValidationError):
        settings.LLM_MODEL = "other"


def test_settings_claude_cli_provider_does_not_require_lm_studio_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("LLM_PROVIDER", "claude_cli")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.LLM_PROVIDER == "claude_cli"
    assert settings.LLM_BASE_URL is None
    assert settings.CLAUDE_CLI_BIN == "claude"


def test_settings_lm_studio_provider_requires_url_model_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("LLM_PROVIDER", "lm_studio")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_mcp_servers_defaults_empty_boots_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """No manifest ⇒ no MCP servers (the optional, boot-green gate)."""

    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("LLM_PROVIDER", "claude_cli")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.MCP_SERVERS == []
    assert settings.mcp_server_configs() == ()
    assert settings.MCP_CALL_TIMEOUT_SECONDS == 30.0


def test_mcp_servers_parses_json_env_into_typed_configs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """``MCP_SERVERS`` JSON env value parses into typed configs + curation."""

    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv("LLM_PROVIDER", "claude_cli")
    monkeypatch.setenv(
        "MCP_SERVERS",
        (
            '[{"name": "weather", "transport": "stdio", "command": "uvx", '
            '"expose": ["get_forecast"], "tools": {"get_forecast": '
            '{"description_fr": "Donne la météo.", "args": ["city"], '
            '"tags": ["météo"], "terminal": true}}}]'
        ),
    )

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    (cfg,) = settings.mcp_server_configs()
    assert cfg.name == "weather"
    assert cfg.expose == ("get_forecast",)
    override = cfg.tools["get_forecast"]
    assert override.description_fr == "Donne la météo."
    assert override.args == ("city",)
    assert override.tags == ("météo",)
    assert override.terminal is True
