// DataCard.tsx — one « DONNÉES GÉNÉRÉES » artefact card (PRD 0014 / issue 0087).
//
// Port of the mockup `mem-card` (Design Mockup/p3d-panels.jsx `DataField` +
// Design Mockup/p3d.css). One card per deliverable: type icon, title, sub,
// type label, a left tick + rank number, and the `is-fresh` pop animation on
// arrival. Click opens the existing SectionsOverlay (the parent threads the
// handler + flips the store entry `fresh → seen`).
//
// Pure presentational: it takes a projected `DeliverableCard` + display chrome
// (rank, fresh, onOpen) and renders. No store access here.

import type { ReactNode } from "react";
import type { DeliverableCard, DeliverableCardType } from "../../lib/deliverableCard";

/** Type → icon, ported verbatim from the mockup `ICONS` map. `composite` is a
 * stacked-layers glyph for a heterogeneous deliverable (the only type the
 * mockup didn't have — the rest are 1:1). Each is a 16×16 line/solid SVG the
 * `.art-icon` box tints with `--accent`. */
const TYPE_ICON: Record<DeliverableCardType, ReactNode> = {
  mail: (
    <svg viewBox="0 0 16 16">
      <title>Courriel</title>
      <rect x="1.5" y="3.5" width="13" height="9" rx="1.2" />
      <path d="M2 4l6 4.5L14 4" />
    </svg>
  ),
  doc: (
    <svg viewBox="0 0 16 16">
      <title>Document</title>
      <path d="M4 1.5h5l3 3v10h-8z" />
      <path d="M9 1.5v3h3" />
      <path d="M5.6 8h5M5.6 10.4h5" />
    </svg>
  ),
  video: (
    <svg viewBox="0 0 16 16">
      <title>Vidéo</title>
      <rect x="1.5" y="3.5" width="9" height="9" rx="1.2" />
      <path d="M10.5 6.5l4-2v7l-4-2z" />
    </svg>
  ),
  contact: (
    <svg viewBox="0 0 16 16">
      <title>Contact</title>
      <circle cx="8" cy="5.5" r="2.6" />
      <path d="M3 13.5c0-2.8 2.2-4.4 5-4.4s5 1.6 5 4.4" />
    </svg>
  ),
  action: (
    <svg viewBox="0 0 16 16">
      <title>Action</title>
      <path d="M8.5 1.5L3 9h4l-.5 5.5L13 7H9z" />
    </svg>
  ),
  composite: (
    <svg viewBox="0 0 16 16">
      <title>Composite</title>
      <path d="M8 1.8l5.5 3-5.5 3-5.5-3z" />
      <path d="M2.5 8l5.5 3 5.5-3" />
      <path d="M2.5 11l5.5 3 5.5-3" />
    </svg>
  ),
};

/** Type → uppercase label under the title, mirroring the mockup
 * `DATA_TYPE_LABEL`. */
const TYPE_LABEL: Record<DeliverableCardType, string> = {
  mail: "COURRIEL",
  doc: "DOCUMENT",
  video: "VIDÉO",
  contact: "CONTACT",
  action: "ACTION",
  composite: "COMPOSITE",
};

type DataCardProps = {
  /** The projected card (title / sub / type / sections). */
  card: DeliverableCard;
  /** 0-based position in the dock (newest = 0), drives the rank label. */
  rank: number;
  /** `true` while the deliverable is still unseen — drives the arrival pop. */
  fresh: boolean;
  /** Open the SectionsOverlay with this card's sections (also flips `seen`).
   * Always provided by the dock; the card is interactive whenever set. */
  onOpen: () => void;
};

export function DataCard({ card, rank, fresh, onOpen }: DataCardProps) {
  return (
    <div
      className={`mem-card p3d-data-card a-${card.type} ${fresh ? "is-fresh" : ""}`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
      // biome-ignore lint/a11y/useSemanticElements: the card is the styled mockup chrome (`.mem-card` with an absolute tick / glow / rank overlay), not a <button>; the role only adds button semantics while Enter/Space are wired via onKeyDown above.
      role="button"
      tabIndex={0}
      title={`Ouvrir « ${card.title} »`}
    >
      <span className="mem-tick" />
      <div className="art-glow" />
      <div className="art-row">
        <span className="art-icon">{TYPE_ICON[card.type]}</span>
        <span className="art-text">
          <span className="art-title">{card.title}</span>
          <span className="art-sub">{card.sub}</span>
        </span>
      </div>
      <span className="art-type">{TYPE_LABEL[card.type]}</span>
      <span className="mem-rank">{String(rank + 1).padStart(2, "0")}</span>
    </div>
  );
}
