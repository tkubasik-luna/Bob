"""Shared test setup: populate required env vars before any ``bob.*`` import.

Importing :mod:`bob.main` triggers ``configure_logging()`` which reads
:func:`bob.config.get_settings`. Those settings demand ``LLM_*`` env vars at
runtime — the test process never has a real ``.env``, so we inject safe
placeholders here.

``BOB_DATA_DIR`` is also pointed at a per-process tmp directory so the
SQLite-backed Jarvis thread never touches the user's real ``~/.bob`` while
running tests.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="bob-test-"))
os.environ["BOB_DATA_DIR"] = str(_TEST_DATA_DIR)

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1234/v1")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("LLM_API_KEY", "test-key")

# Tests manage their own DB lifecycle via the ``clear_jarvis_history`` fixture
# and expect bob.db to persist across lifespan reuse inside a single test
# (e.g. seeding state outside the TestClient, then starting it). Disable the
# auto-wipe so existing fixtures stay valid.
os.environ.setdefault("BOB_CLEAR_ON_START", "false")


@pytest.fixture()
def clear_jarvis_history() -> Iterator[None]:
    """Reset Jarvis persistence on disk before AND after a test.

    Removes ``bob.db`` from the test data dir so the next lifespan startup
    sees a clean slate. Tests that need pre-seeded history must run
    ``with TestClient(app) as client:`` first to trigger the lifespan and
    then mutate the store inside that block.
    """

    db_path = _TEST_DATA_DIR / "bob.db"
    db_path.unlink(missing_ok=True)
    try:
        yield
    finally:
        db_path.unlink(missing_ok=True)
