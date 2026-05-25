"""Golden-prompt snapshot helpers (no external deps).

Issue 0043 introduces a tiny inline snapshot harness for the
:class:`bob.context.assembler.ContextAssembler` output. We deliberately
avoid the ``syrupy`` dependency: the assembled prompt is a plain list of
chat-message dicts, JSON-serialisable, so a hand-rolled writer keeps the
test suite hermetic and is trivial to read in PR diffs.

Usage:

    from tests._harness.golden_prompt import (
        FIXTURES_DIR,
        load_transcript_fixture,
        assert_matches_snapshot,
    )

    transcript = load_transcript_fixture("simple_two_turns")
    seed_history(jarvis_store, transcript)
    actual = assembler.assemble(user_message=transcript["pending_user_message"])
    assert_matches_snapshot(actual, "simple_two_turns_prompt")

On first run (snapshot file missing) the helper writes the snapshot and
fails the test loudly so the author commits the file consciously. Set
``BOB_UPDATE_SNAPSHOTS=1`` to overwrite an existing snapshot during
development.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: Snapshots + transcript fixtures live in a single ``prompts/`` directory.
#: Snapshots end with ``.snapshot.json``, transcripts with ``.transcript.json``.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "prompts"


def _ensure_fixtures_dir() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)


def load_transcript_fixture(name: str) -> dict[str, Any]:
    """Load a transcript fixture from ``tests/fixtures/prompts/{name}.transcript.json``.

    Transcript shape::

        {
          "system_content": "...",
          "history": [{"role": "user", "content": "..."}, ...],
          "pending_user_message": "..."
        }
    """

    path = FIXTURES_DIR / f"{name}.transcript.json"
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise AssertionError(
            f"Transcript fixture {name!r} must be a JSON object, got {type(payload).__name__}"
        )
    return payload


def assert_matches_snapshot(actual: Any, name: str) -> None:
    """Assert ``actual`` round-trips through the snapshot file ``name``.

    First run (file missing) writes the snapshot then raises so the author
    reviews it before committing. ``BOB_UPDATE_SNAPSHOTS=1`` forces a
    rewrite on every run during development.
    """

    _ensure_fixtures_dir()
    path = FIXTURES_DIR / f"{name}.snapshot.json"
    serialised = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    if os.environ.get("BOB_UPDATE_SNAPSHOTS") == "1":
        path.write_text(serialised, encoding="utf-8")
        return

    if not path.exists():
        path.write_text(serialised, encoding="utf-8")
        raise AssertionError(
            f"Snapshot {name!r} did not exist; created at {path}. Re-run the test to validate."
        )

    expected = path.read_text(encoding="utf-8")
    if expected != serialised:
        raise AssertionError(
            f"Snapshot mismatch for {name!r}.\n"
            f"--- expected ({path}) ---\n{expected}\n"
            f"--- actual ---\n{serialised}\n"
            f"Set BOB_UPDATE_SNAPSHOTS=1 if the change is intentional."
        )


def seed_history(
    jarvis_store: Any,
    transcript: dict[str, Any],
    *,
    append_pending: bool = True,
) -> None:
    """Replay ``transcript`` into ``jarvis_store``.

    Appends every entry in ``transcript['history']`` and, by default, the
    pending user message at the end (matching the orchestrator which
    persists the user turn *before* invoking the assembler).
    """

    for msg in transcript.get("history", []):
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise AssertionError(
                f"Transcript history entry must carry string role+content, got {msg!r}"
            )
        action = msg.get("action")
        action_value = action if action in ("done", "ask_user", "progress") else None
        jarvis_store.append(role, content, action=action_value)
    if append_pending:
        pending = transcript.get("pending_user_message")
        if isinstance(pending, str) and pending:
            jarvis_store.append("user", pending)
