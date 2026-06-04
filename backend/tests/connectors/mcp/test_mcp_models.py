"""Unit tests for :mod:`bob.connectors.mcp.models` pure helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bob.connectors.mcp.models import MCPServerConfig, extract_text_content


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
