"""Tests for :mod:`bob.validation.reason_codes` + the generated TS table.

The PRD requires the registry to be versioned and i18n-ready for the
frontend. This test enforces both:

- ``REASON_CODE_SCHEMA_VERSION`` is exposed and stable;
- every legacy reason-code constant the runner used to expose
  (``REASON_OK`` etc.) is still present in the central registry;
- the generated ``frontend/src/generated/reason_codes.ts`` mirrors the
  Python source-of-truth byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

from bob.validation.reason_codes import (
    DEFAULT_REGISTRY,
    REASON_CODE_SCHEMA_VERSION,
    REASON_HARD_KILLED,
    REASON_INVALID_OUTPUT,
    REASON_ITERATION_CAP,
    REASON_LLM_FAILED,
    REASON_OK,
    REASON_TOKEN_CAP,
    REASON_TOOL_FAILED,
    REASON_UNKNOWN_TASK,
    REASON_USER_CANCELLED,
    REASON_VALIDATION_EXHAUSTED,
    REASON_WALL_CLOCK_CAP,
    ReasonCodeRegistry,
    render_frontend_table_ts,
)


def test_schema_version_is_one() -> None:
    assert REASON_CODE_SCHEMA_VERSION == 1


def test_all_legacy_constants_are_registered() -> None:
    """The legacy constants (PRD 0045 export surface) live in the registry."""

    legacy = {
        REASON_OK,
        REASON_ITERATION_CAP,
        REASON_WALL_CLOCK_CAP,
        REASON_TOKEN_CAP,
        REASON_USER_CANCELLED,
        REASON_HARD_KILLED,
        REASON_INVALID_OUTPUT,
        REASON_LLM_FAILED,
        REASON_TOOL_FAILED,
        REASON_VALIDATION_EXHAUSTED,
        REASON_UNKNOWN_TASK,
    }
    registry_codes = {entry.code for entry in DEFAULT_REGISTRY}
    missing = legacy - registry_codes
    assert missing == set(), f"reason codes missing from registry: {missing}"


def test_registry_has_lookup_by_code() -> None:
    entry = DEFAULT_REGISTRY.get("invalid_output")
    assert entry is not None
    assert entry.code == "invalid_output"
    assert entry.actor in ("shared", "jarvis", "sub_agent")


def test_registry_iteration_order_is_stable() -> None:
    """Re-iterating yields the same sequence (used by the frontend table)."""

    first = [entry.code for entry in DEFAULT_REGISTRY]
    second = [entry.code for entry in DEFAULT_REGISTRY]
    assert first == second


def test_render_frontend_table_contains_schema_version_and_all_codes() -> None:
    rendered = render_frontend_table_ts()
    assert "REASON_CODE_SCHEMA_VERSION = 1" in rendered
    assert "REASON_CODES" in rendered
    # Biome-style TS object literal: unquoted keys + double-quoted strings
    # + trailing commas (see ``render_frontend_table_ts`` docstring).
    for entry in DEFAULT_REGISTRY:
        assert f'code: "{entry.code}"' in rendered


def test_generated_frontend_table_exists_and_matches_source_of_truth() -> None:
    """The committed ``reason_codes.ts`` mirrors the Python registry.

    This is the i18n bridge AC from issue 0048 — the frontend file is
    machine-generated and tracked, but the test asserts it has not
    drifted from the Python source. If this test fails, regenerate the
    file via :func:`bob.validation.reason_codes.write_frontend_table`.
    """

    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    target = repo_root / "frontend" / "src" / "generated" / "reason_codes.ts"
    assert target.exists(), f"missing generated table at {target}"
    on_disk = target.read_text(encoding="utf-8")
    rendered = render_frontend_table_ts()
    assert on_disk == rendered, (
        "frontend/src/generated/reason_codes.ts is stale; regenerate via "
        "bob.validation.reason_codes.write_frontend_table"
    )


def test_construct_custom_registry() -> None:
    """A second registry instance picks up the same backing entries."""

    custom = ReasonCodeRegistry()
    assert len(custom) == len(DEFAULT_REGISTRY)
