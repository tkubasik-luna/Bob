"""Shared test setup: populate required env vars before any ``bob.*`` import.

Importing :mod:`bob.main` triggers ``configure_logging()`` which reads
:func:`bob.config.get_settings`. Those settings demand ``LLM_*`` env vars at
runtime — the test process never has a real ``.env``, so we inject safe
placeholders here.
"""

from __future__ import annotations

import os

os.environ.setdefault("LLM_BASE_URL", "http://localhost:1234/v1")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("LLM_API_KEY", "test-key")
