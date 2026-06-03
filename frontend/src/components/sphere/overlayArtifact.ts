// overlayArtifact.ts — pure helpers backing the SectionsOverlay chrome (PRD
// 0014 / issue 0088). Kept dependency-free + side-effect-free so they're unit
// testable in isolation: header chip/label/ref derivation and the plain-text
// extraction the "LIRE À VOIX HAUTE" action feeds to TTS.

import type { ComponentDescriptor } from "../../types/ws";

/** Per-type chip label shown in the mono header, mirroring the mockup
 * `OV_CHIP` map (`Design Mockup/p3d-overlay.jsx`). A composite stack mixing
 * types — or an unknown single type — falls back to `SURFACE`. */
const CHIP_BY_COMPONENT: Record<string, string> = {
  Markdown: "FICHIER",
  Mail: "BOÎTE",
};

/** Header chip text for a stack of sections. A single-type stack shows that
 * type's chip; anything mixed (or unknown) collapses to the generic `SURFACE`
 * so the chrome reads sensibly for composite deliverables (feature 0011). */
export function overlayChip(sections: ComponentDescriptor[]): string {
  const kinds = new Set(sections.map((s) => s.component));
  if (kinds.size === 1) {
    const only = sections[0]?.component ?? "";
    return CHIP_BY_COMPONENT[only] ?? "SURFACE";
  }
  return "SURFACE";
}

/** Stable REF marker for the header (`RÉF · SEC-XXXX`). Derived from the section
 * components + count via a deterministic FNV-1a hash so it stays constant per
 * payload (no flaky snapshots, no re-roll on every render). Purely cosmetic —
 * collision likelihood doesn't matter. */
export function overlayRefMarker(sections: ComponentDescriptor[]): string {
  const sample = `${sections.length}:${sections.map((s) => s.component).join(",")}`.slice(0, 64);
  let hash = 0x811c9dc5;
  for (let i = 0; i < sample.length; i++) {
    hash ^= sample.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  const hex = (hash >>> 0).toString(16).slice(-4).toUpperCase();
  return hex.padStart(4, "0");
}

/** Flatten a section stack to the plain text "LIRE À VOIX HAUTE" reads aloud.
 *
 * Per-type extraction keeps the spoken output natural: a Markdown/Document
 * section reads its raw `content`; a Mail reads sender → subject → body. Unknown
 * sections contribute nothing (rather than dumping their raw prop bag). Empty
 * input yields an empty string so the caller can skip speaking. */
export function overlaySpeechText(sections: ComponentDescriptor[]): string {
  const parts: string[] = [];
  for (const section of sections) {
    const props = section.props as Record<string, unknown>;
    if (section.component === "Markdown") {
      const content = typeof props.content === "string" ? props.content : "";
      const stripped = stripMarkdown(content);
      if (stripped.length > 0) parts.push(stripped);
    } else if (section.component === "Mail") {
      const from = props.from as { name?: unknown } | undefined;
      const name = from && typeof from.name === "string" ? from.name : "";
      const subject = typeof props.subject === "string" ? props.subject : "";
      const body = typeof props.bodyPreview === "string" ? props.bodyPreview : "";
      const lead = [name ? `Courriel de ${name}.` : "", subject, body]
        .filter((s) => s.length > 0)
        .join(" ");
      if (lead.length > 0) parts.push(lead);
    }
  }
  return parts.join("\n\n").trim();
}

/** Strip the common Markdown syntax so TTS doesn't read `#`, `*`, backticks,
 * etc. aloud. Coarse on purpose — readability over fidelity. */
function stripMarkdown(src: string): string {
  return src
    .replace(/```[\s\S]*?```/g, " ") // fenced code blocks
    .replace(/`([^`]+)`/g, "$1") // inline code
    .replace(/^#{1,6}\s+/gm, "") // ATX headings
    .replace(/^\s*>\s?/gm, "") // blockquotes
    .replace(/^\s*[-*+]\s+/gm, "") // unordered list markers
    .replace(/^\s*\d+\.\s+/gm, "") // ordered list markers
    .replace(/\*\*([^*]+)\*\*/g, "$1") // bold
    .replace(/\*([^*]+)\*/g, "$1") // italic
    .replace(/_([^_]+)_/g, "$1") // italic (underscore)
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1") // links → label
    .replace(/^-{3,}\s*$/gm, "") // horizontal rules
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
