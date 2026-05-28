"""External-service connectors for Bob.

Each connector is a self-contained sub-package living under
:mod:`bob.connectors` (e.g. :mod:`bob.connectors.gmail`). A connector owns
its authentication, query language, and domain model — it is independent of
the tool-calling layer (``bob.tools``) and the UI registry
(``bob.ui_registry``). Tool handlers wire connectors to the LLM; the
connector itself never imports tool or UI code.
"""

from __future__ import annotations
