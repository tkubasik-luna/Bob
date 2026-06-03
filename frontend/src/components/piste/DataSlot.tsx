// DataSlot.tsx — Piste 3D · Nacre right slot (PRD 0014 / issue 0087).
//
// The right slot of the 3D stage (tilted −22° into depth by `.layout-depth
// .slot-data`). It renders the « DONNÉES GÉNÉRÉES » dock (`<DataDock/>`), which
// collects every deliverable the session generates (sub-task results + Bob's
// own ui_payload) into one card per artefact. Clicking a card opens the
// existing SectionsOverlay via the `onOpenDeliverable` prop and flips the
// store entry `fresh → seen` (handled inside the dock).
//
// DataSlot itself stays THIN: the store + ingestion + projection live in
// `store/deliverableStore.ts`, `lib/deliverableCard.ts`, and
// `useDeliverableIngest.ts`. This component only wires the slot to the dock and
// forwards the overlay-open callback SphereUI handed down.

import type { ComponentDescriptor } from "../../types/ws";
import { DataDock } from "./DataDock";

type DataSlotProps = {
  /** Open the SectionsOverlay on a card click with the card's stored
   * deliverable sections. Wired by SphereUI to its `openOverlay` callback;
   * threaded straight through to the dock's card clicks. */
  onOpenDeliverable?: (sections: ComponentDescriptor[]) => void;
};

export function DataSlot({ onOpenDeliverable }: DataSlotProps) {
  return <DataDock onOpenDeliverable={onOpenDeliverable} />;
}
