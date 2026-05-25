"""UserMessageProvider — emits the live user turn for the bounded policy.

PRD 0006 / issue 0046. The bounded policy decouples prompt composition
from persistence: the orchestrator no longer needs to append the user
turn to :class:`JarvisStore` *before* assembly. Instead the live user
message flows through :class:`AssemblyContext.user_message` and this
provider emits it as the final ``role=user`` entry.

The orchestrator can still persist the user turn (it does, before calling
assemble); that part is unchanged. The provider intentionally does not
look at ``JarvisStore`` so the bounded prompt remains immune to the
persist-before-assemble race that pre-0046 code coupled into.

When ``ctx.user_message`` is ``None`` or empty the provider emits no
entry — the orchestrator's smoke / sub-task flows can re-use the same
provider list without a special branch.
"""

from __future__ import annotations

from collections.abc import Sequence

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry
from bob.context.provider import AssemblyContext

#: Stable id for this provider.
USER_MESSAGE_PROVIDER_ID = "user_message"


class UserMessageProvider:
    """Emit a single ``role=user`` :class:`ContextEntry` for the live turn.

    Stateless — all input comes from :class:`AssemblyContext`.
    """

    @property
    def provider_id(self) -> str:
        return USER_MESSAGE_PROVIDER_ID

    def entries(self, ctx: AssemblyContext) -> Sequence[ContextEntry]:
        message = ctx.user_message
        if not isinstance(message, str) or not message:
            return []
        return [
            ContextEntry(
                id=f"{USER_MESSAGE_PROVIDER_ID}:live",
                kind="user_turn",
                source="orchestrator",
                token_estimate=len(message) // 4,
                pinned=False,
                created_at="",
                provider_id=USER_MESSAGE_PROVIDER_ID,
                payload={"role": "user", "content": message},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        ]
