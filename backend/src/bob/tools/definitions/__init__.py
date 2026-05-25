"""Built-in :class:`ToolDefinition` implementations.

Each tool lives in its own module so future additions (``addendum_task``,
``replan_task``, ``say``, sub-agent-side ``web_search`` …) follow the
same one-file-per-tool layout.

Currently shipped tools (v1):

- :mod:`bob.tools.definitions.spawn` — ``spawn_subtask``.
- :mod:`bob.tools.definitions.forward` — ``forward_to_subtask``.
- :mod:`bob.tools.definitions.cancel` — ``cancel_subtask``.
"""
