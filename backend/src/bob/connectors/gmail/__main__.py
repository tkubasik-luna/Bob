"""Module CLI entry for ``python -m bob.connectors.gmail``.

Equivalent to ``python -m bob.connectors.gmail.auth`` — kept here so the
canonical package-level invocation also works (``python -m
bob.connectors.gmail``) without the ``runpy`` warning about a module
already being in :data:`sys.modules` (the package's ``__init__`` imports
:mod:`bob.connectors.gmail.auth`, which trips that warning when running
``python -m bob.connectors.gmail.auth`` directly).
"""

from __future__ import annotations

import sys

from bob.connectors.gmail.auth import _main

if __name__ == "__main__":
    sys.exit(_main())
