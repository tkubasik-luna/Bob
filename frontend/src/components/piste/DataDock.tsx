// DataDock.tsx — the « DONNÉES GÉNÉRÉES » dock in the right slot (PRD 0014 /
// issue 0087).
//
// Port of the mockup `DataField` (Design Mockup/p3d-panels.jsx) adapted to read
// the REAL session deliverables from `deliverableStore` instead of the mockup's
// decaying demo pool. One card per stored deliverable, newest at the top, each
// projected by the pure `toCard`. A header counts the ACTIVE (still-`fresh`,
// unseen) artefacts. Clicking a card opens the existing SectionsOverlay (via
// the `onOpenDeliverable` prop threaded from SphereUI) and flips the entry to
// `seen`.
//
// Retention: NOTHING auto-evicts — the dock holds the whole session's
// deliverables and simply scrolls / stacks. Cards animate in (`is-fresh`) and
// persist.

import { useMemo } from "react";
import { type DeliverableCard, toCard } from "../../lib/deliverableCard";
import {
  type DeliverableEntry,
  selectActiveCount,
  selectOrdered,
  useDeliverableStore,
} from "../../store/deliverableStore";
import type { ComponentDescriptor } from "../../types/ws";
import { DataCard } from "./DataCard";
import "./DataDock.css";
import { useDeliverableIngest } from "./useDeliverableIngest";

// Vertical plateau geometry, ported from the mockup (`MEM_TOP` / `MEM_STEP`):
// the newest card sits at the top, each older one a step further down.
const MEM_TOP = 14;
const MEM_STEP = 80;

type DataDockProps = {
  /** Open the SectionsOverlay with a card's stored sections. Threaded from
   * SphereUI's single overlay-open callback through DataSlot. Optional so the
   * dock still renders standalone (e.g. in isolation), in which case clicks are
   * inert. */
  onOpenDeliverable?: (sections: ComponentDescriptor[]) => void;
};

export function DataDock({ onOpenDeliverable }: DataDockProps) {
  // Bridge the live event sources into the store (task results + Bob ui_payload).
  useDeliverableIngest();

  const byId = useDeliverableStore((s) => s.byId);
  const markSeen = useDeliverableStore((s) => s.markSeen);

  // Newest-first, and the count of still-fresh (active) artefacts. Memoised on
  // `byId` so a `markSeen` toggle only re-renders on a real change.
  const ordered = useMemo<DeliverableEntry[]>(() => selectOrdered(byId), [byId]);
  const activeCount = useMemo(() => selectActiveCount(byId), [byId]);
  const total = ordered.length;

  // Project each stored deliverable once. Pairs the pure card with the entry's
  // id + live `fresh` status so the row can wire its click + animation.
  const cards = useMemo(
    () =>
      ordered.map((entry) => ({
        id: entry.id,
        fresh: entry.status === "fresh",
        sections: entry.deliverable,
        card: toCard(entry.deliverable, entry.task) satisfies DeliverableCard,
      })),
    [ordered],
  );

  // Idle: no deliverables yet → render nothing, matching the empty-slot
  // foundation behaviour (the dock only appears once Bob has generated data).
  if (total === 0) return null;

  return (
    <div className="panel data-panel mem-dock p3d-data-dock">
      <div className="panel-head">
        <span className="panel-dot" data-live={activeCount > 0} />
        <span className="panel-title">DONNÉES GÉNÉRÉES</span>
        <span className="panel-phase">
          {String(activeCount).padStart(2, "0")} / {String(total).padStart(2, "0")} actives
        </span>
      </div>
      <div className="data-field mem-field">
        <div className="mem-rail" />
        {cards.map(({ id, fresh, sections, card }, rank) => (
          <div
            key={id}
            className="p3d-data-card-slot"
            style={{ top: MEM_TOP + rank * MEM_STEP, zIndex: 100 - rank }}
          >
            <DataCard
              card={card}
              rank={rank}
              fresh={fresh}
              onOpen={() => {
                // Open the existing overlay with this deliverable's stack, then
                // flip the entry fresh → seen so the active counter drops and
                // the card stops animating.
                onOpenDeliverable?.(sections);
                markSeen(id);
              }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
