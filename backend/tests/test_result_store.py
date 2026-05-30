"""Unit tests for :mod:`bob.sub_agent.result_store` (PRD 0009 / P1).

Covers the per-run blackboard contract: sequential refs, lookup by ref and
``last()``, the default (un-projected) projection that preserves prior
behaviour, a custom projector, and the must-not-break-the-run tolerance of a
raising projector.
"""

from __future__ import annotations

import json

from bob.sub_agent.result_store import (
    ProjectedResult,
    StoredResult,
    ToolResultStore,
    default_projector,
)


def test_default_projector_preserves_full_result_as_digest() -> None:
    result = {"query": "label:INBOX", "count": 1, "messages": [{"subject": "hi"}]}
    proj = default_projector(result)
    # Un-projected tools must behave exactly as before PRD 0009: the digest IS
    # the full result, there is no structured deliverable, and it never
    # converges.
    assert proj.digest == result
    assert proj.deliverable is None
    assert proj.terminal is False
    # Summary is the compact JSON the old salvage path produced.
    assert proj.summary == json.dumps(result, ensure_ascii=False, sort_keys=True)


def test_default_projector_caps_summary_length() -> None:
    big = {"blob": "x" * 5000}
    proj = default_projector(big)
    assert len(proj.summary) <= 2001  # cap + the ellipsis char
    assert proj.summary.endswith("…")
    # The digest is NOT truncated — only the human-facing summary is.
    assert proj.digest == big


def test_default_projector_tolerates_non_json_result() -> None:
    # default=str is not used here; a value json cannot serialise falls back to
    # repr rather than raising.
    result = {"obj": object()}
    proj = default_projector(result)
    assert isinstance(proj.summary, str)
    assert proj.summary  # non-empty


def test_put_assigns_sequential_refs_per_tool() -> None:
    store = ToolResultStore()
    a = store.put(tool_name="gmail_search", tool_version="v1", result={"count": 0})
    b = store.put(tool_name="gmail_search", tool_version="v1", result={"count": 1})
    c = store.put(tool_name="web_search", tool_version="v1", result={"hits": 3})
    assert a.ref == "gmail_search#1"
    assert b.ref == "gmail_search#2"
    assert c.ref == "web_search#1"
    assert len(store) == 3


def test_get_resolves_ref_and_handles_missing() -> None:
    store = ToolResultStore()
    stored = store.put(tool_name="gmail_search", tool_version="v1", result={"count": 1})
    assert store.get("gmail_search#1") is stored
    assert store.get("gmail_search#99") is None
    assert store.get(None) is None
    assert store.get("") is None


def test_last_returns_most_recent_across_tools() -> None:
    store = ToolResultStore()
    assert store.last() is None
    store.put(tool_name="gmail_search", tool_version="v1", result={"count": 1})
    last = store.put(tool_name="web_search", tool_version="v1", result={"hits": 2})
    assert store.last() is last
    assert store.last() is not None
    assert store.last().ref == "web_search#1"


def test_custom_projector_is_used() -> None:
    def projector(result: dict[str, object]) -> ProjectedResult:
        return ProjectedResult(
            digest={"count": result["count"]},
            deliverable={"component": "Mail", "props": {"subject": "x"}},
            summary="one mail",
            terminal=True,
        )

    store = ToolResultStore()
    stored = store.put(
        tool_name="gmail_search",
        tool_version="v1",
        result={"count": 1, "messages": [{"subject": "x", "bodyPreview": "secret"}]},
        projector=projector,
    )
    # The full raw result is retained server-side …
    assert "bodyPreview" in stored.result["messages"][0]
    # … but the digest the projector chose is compact and body-free.
    assert stored.projection.digest == {"count": 1}
    assert stored.projection.deliverable == {"component": "Mail", "props": {"subject": "x"}}
    assert stored.projection.terminal is True


def test_raising_projector_falls_back_to_default() -> None:
    def boom(_result: dict[str, object]) -> ProjectedResult:
        raise ValueError("projector bug")

    store = ToolResultStore()
    result = {"count": 1}
    stored = store.put(tool_name="gmail_search", tool_version="v1", result=result, projector=boom)
    # A buggy projector must not abort the run — it degrades to the default.
    assert stored.projection.digest == result
    assert stored.projection.deliverable is None
    assert stored.projection.terminal is False


def test_stored_result_carries_tool_identity() -> None:
    store = ToolResultStore()
    stored = store.put(tool_name="gmail_search", tool_version="v1", result={"count": 0})
    assert isinstance(stored, StoredResult)
    assert stored.tool_name == "gmail_search"
    assert stored.tool_version == "v1"
