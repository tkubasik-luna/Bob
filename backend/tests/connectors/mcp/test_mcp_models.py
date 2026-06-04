"""Unit tests for :mod:`bob.connectors.mcp.models` pure helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bob.connectors.mcp.models import (
    MCPServerConfig,
    MCPToolOverride,
    extract_text_content,
    parse_mcp_servers,
)


@dataclass
class _Block:
    text: str | None = None
    type: str = "text"


@dataclass
class _Result:
    content: Any
    isError: bool = False


def test_extract_joins_text_blocks() -> None:
    result = _Result(content=[_Block(text="hello"), _Block(text="world")])
    text, is_error = extract_text_content(result)
    assert text == "hello\n\nworld"
    assert is_error is False


def test_extract_reports_is_error_flag() -> None:
    result = _Result(content=[_Block(text="boom")], isError=True)
    text, is_error = extract_text_content(result)
    assert text == "boom"
    assert is_error is True


def test_extract_non_text_block_keeps_placeholder() -> None:
    result = _Result(content=[_Block(text=None, type="image")])
    text, _ = extract_text_content(result)
    assert text == "[image]"


def test_extract_empty_content() -> None:
    text, is_error = extract_text_content(_Result(content=[]))
    assert text == ""
    assert is_error is False


def test_extract_tolerates_missing_attrs() -> None:
    # An object exposing neither content nor isError yields ("", False).
    text, is_error = extract_text_content(object())
    assert text == ""
    assert is_error is False


def test_server_config_defaults() -> None:
    cfg = MCPServerConfig(name="demo")
    assert cfg.transport == "stdio"
    assert cfg.args == ()
    assert cfg.url is None
    # Issue 0094 — manifest defaults: no allowlist (everything exposed), no
    # per-tool overrides.
    assert cfg.expose is None
    assert cfg.tools == {}


# --- manifest curation accessors (issue 0094) -------------------------------


def test_is_exposed_no_allowlist_exposes_everything() -> None:
    cfg = MCPServerConfig(name="demo")
    assert cfg.is_exposed("anything") is True


def test_is_exposed_allowlist_filters() -> None:
    cfg = MCPServerConfig(name="demo", expose=("get_forecast",))
    assert cfg.is_exposed("get_forecast") is True
    assert cfg.is_exposed("get_alerts") is False


def test_override_for_returns_declared_override() -> None:
    override = MCPToolOverride(description_fr="Météo.", tags=("météo",))
    cfg = MCPServerConfig(name="demo", tools={"get_forecast": override})
    assert cfg.override_for("get_forecast") is override
    assert cfg.override_for("missing") is None


# --- parse_mcp_servers (issue 0094) -----------------------------------------


def test_parse_empty_or_absent_yields_empty() -> None:
    assert parse_mcp_servers([]) == ()
    assert parse_mcp_servers(None) == ()
    assert parse_mcp_servers("not a list") == ()


def test_parse_full_entry() -> None:
    raw = [
        {
            "name": "weather",
            "transport": "stdio",
            "command": "uvx",
            "args": ["weather-mcp", "--verbose"],
            "env": {"API_KEY": "abc"},
            "expose": ["get_forecast"],
            "tools": {
                "get_forecast": {
                    "description_fr": "Donne la météo d'une ville.",
                    "args": ["city"],
                    "tags": ["météo", "weather"],
                    "terminal": True,
                }
            },
        }
    ]
    (cfg,) = parse_mcp_servers(raw)
    assert cfg.name == "weather"
    assert cfg.transport == "stdio"
    assert cfg.command == "uvx"
    assert cfg.args == ("weather-mcp", "--verbose")
    assert cfg.env == {"API_KEY": "abc"}
    assert cfg.expose == ("get_forecast",)
    override = cfg.tools["get_forecast"]
    assert override.description_fr == "Donne la météo d'une ville."
    assert override.args == ("city",)
    assert override.tags == ("météo", "weather")
    assert override.terminal is True


def test_parse_http_entry() -> None:
    (cfg,) = parse_mcp_servers([{"name": "remote", "transport": "http", "url": "https://x/mcp"}])
    assert cfg.transport == "http"
    assert cfg.url == "https://x/mcp"
    assert cfg.command is None


def test_parse_is_lenient_drops_malformed_entries() -> None:
    """A malformed entry is dropped; well-formed peers survive (boot-green)."""

    raw = [
        "not a dict",
        {"transport": "stdio"},  # no name
        {"name": ""},  # empty name
        {"name": "good"},  # survives
        {"name": "bad-transport", "transport": "carrier-pigeon"},  # transport coerced
    ]
    configs = parse_mcp_servers(raw)
    names = [c.name for c in configs]
    assert names == ["good", "bad-transport"]
    # Unknown transport falls back to the safe stdio default.
    assert configs[1].transport == "stdio"


def test_parse_defaults_for_missing_override_fields() -> None:
    (cfg,) = parse_mcp_servers([{"name": "s", "tools": {"t": {"description_fr": "x"}}}])
    override = cfg.tools["t"]
    assert override.description_fr == "x"
    assert override.args is None  # absent → keep full schema
    assert override.tags == ()
    assert override.terminal is False
