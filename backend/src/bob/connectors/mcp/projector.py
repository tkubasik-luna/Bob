"""Generic projection for any MCP tool result (PRD 0015).

:func:`project_mcp_default` is the *single* projector reused by every uncurated
MCP tool — branching a new MCP tool needs zero projector code. It mirrors
:func:`bob.sub_agent.tool_registry.project_web_fetch`: turn the tool's text
content into a capped transcript digest plus a ``Markdown`` deliverable card,
non-terminal by default.

Why ``Markdown`` and not a typed card: the frontend already degrades an unknown
component to a generic doc card, and the ``Markdown`` component is in the
``ui_registry`` (so the descriptor validates). A typed per-tool card is an
opt-in upgrade (curation may point a tool at a dedicated projector later) — out
of scope here.

Pure: no I/O, no SDK import. Consumes the handler's stored result dict
(``{"tool", "text", "is_error"}``) — the SDK→text fold already happened in the
handler via :func:`bob.connectors.mcp.models.extract_text_content`.
"""

from __future__ import annotations

from typing import Any

from bob.sub_agent.result_store import ProjectedResult

#: Cap on the tool text echoed into the transcript digest (the model's working
#: material; the full text is never re-sent — the store holds it server-side).
#: Mirrors ``_WEB_FETCH_DIGEST_CONTENT_CHARS``.
_MCP_DIGEST_TEXT_CHARS = 4000
#: Smaller excerpt rendered in the Markdown card (the anti-stall deliverable),
#: mirroring ``_WEB_FETCH_CARD_EXCERPT_CHARS``.
_MCP_CARD_EXCERPT_CHARS = 1500


def project_mcp_default(result: dict[str, Any]) -> ProjectedResult:
    """Project an MCP tool result into transcript / UI / summary forms.

    - **digest** (→ transcript): ``{tool, text, truncated}`` with ``text`` capped
      at :data:`_MCP_DIGEST_TEXT_CHARS`; ``truncated`` flags when the result was
      longer (so the model knows there is more it cannot see).
    - **deliverable** (→ overlay): a single ``Markdown`` "tool result" card
      (a shorter excerpt). Present so a stall right after the call still shows a
      card instead of an empty overlay (PRD 0010 anti-stall). ``None`` when the
      tool returned no text.
    - **summary** (→ spoken): a deterministic French line naming the tool.
    - **terminal**: ``False`` by default — an uncurated MCP tool feeds a later
      synthesis. Per-tool curation overrides this for single-shot tools (issue
      0094); this generic projector never converges.
    """

    tool = result.get("tool")
    tool = tool if isinstance(tool, str) and tool else "outil"
    text = result.get("text")
    text = text if isinstance(text, str) else ""

    digest_text = text
    truncated = False
    if len(digest_text) > _MCP_DIGEST_TEXT_CHARS:
        digest_text = digest_text[:_MCP_DIGEST_TEXT_CHARS] + "…"
        truncated = True
    digest: dict[str, Any] = {"tool": tool, "text": digest_text, "truncated": truncated}

    stripped = text.strip()
    if stripped:
        excerpt = stripped[:_MCP_CARD_EXCERPT_CHARS]
        if len(stripped) > _MCP_CARD_EXCERPT_CHARS:
            excerpt = excerpt + "…"
        card_md = f"**{tool}**\n\n{excerpt}"
        deliverable: list[dict[str, Any]] | None = [
            {"component": "Markdown", "props": {"content": card_md}}
        ]
        summary = f"Résultat de l'outil « {tool} »."
    else:
        deliverable = None
        summary = f"L'outil « {tool} » n'a renvoyé aucun contenu."

    return ProjectedResult(
        digest=digest,
        deliverable=deliverable,
        summary=summary,
        terminal=False,
    )


__all__ = ["project_mcp_default"]
