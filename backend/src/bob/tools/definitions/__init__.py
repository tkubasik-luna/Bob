"""Built-in :class:`ToolDefinition` implementations.

Each tool lives in its own module so future additions follow the same
one-file-per-tool layout.

Currently shipped tools (v1, Jarvis-side):

- :mod:`bob.tools.definitions.say` — ``say`` (direct reply).
- :mod:`bob.tools.definitions.show_task_result` — recall a stored
  deliverable.
- :mod:`bob.tools.definitions.spawn_task` — ``spawn_task``.
- :mod:`bob.tools.definitions.addendum_task` — ``addendum_task``.
- :mod:`bob.tools.definitions.replan_task` — ``replan_task``.
- :mod:`bob.tools.definitions.cancel_task` — ``cancel_task``.
"""
