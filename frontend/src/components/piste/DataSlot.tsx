// DataSlot.tsx — Piste 3D · Nacre right slot (PRD 0014 / issue 0083).
//
// The right slot of the 3D stage (tilted −22° into depth by `.layout-depth
// .slot-data`). For the foundation slice this is an EMPTY placeholder: the
// data-dock deliverable cards fill it in issue 0087. Rendered inside
// `<div className="slot-data">` by SphereUI.
//
// PROP CONTRACT (declared now so 0087 binds without editing the shell):
// `onOpenDeliverable` is the click-handler a data card calls to open the
// SectionsOverlay with its stored deliverable. SphereUI wires it to its single
// overlay-open callback (`openOverlay`). It is OPTIONAL so the stub renders
// fine without a parent providing it; 0087 starts calling it on card click.

import type { ComponentDescriptor } from "../../types/ws";

type DataSlotProps = {
  /** Open the SectionsOverlay on a card click with the card's stored
   * deliverable sections. Wired by SphereUI to its `openOverlay` callback;
   * issue 0087 invokes it from the rendered data cards. */
  onOpenDeliverable?: (sections: ComponentDescriptor[]) => void;
};

export function DataSlot(_props: DataSlotProps) {
  // Empty placeholder for the foundation slice. The `onOpenDeliverable` prop is
  // declared/accepted so the wiring is in place; issue 0087 renders the cards
  // and calls it. Intentionally unused here (prefixed `_` to satisfy lint).
  return null;
}
