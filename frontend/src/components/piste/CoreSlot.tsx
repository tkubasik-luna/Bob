// CoreSlot.tsx — Piste 3D · Nacre centre slot (PRD 0014 / issue 0083).
//
// The centre slot holds the conscience orb. For the foundation slice it renders
// the EXISTING `<SphereCanvas/>` as a PLACEHOLDER (so the orb still works while
// the shell lands), wrapped in `.core` for the depth sizing, plus the
// `CORE · conscience` label beneath. Issue 0084 swaps the internals for the
// ported nebula orb — so this file's surface is kept minimal: it forwards the
// exact orb props SphereUI already owns (state/variant/motion/glow/theme/mood/
// audioLevelRef) and otherwise just lays out the slot.

import { SphereCanvas, type SphereCanvasProps } from "../../sphere/SphereCanvas";

// The orb prop surface for the core slot is exactly SphereCanvas's prop
// surface — SphereUI derives these once and passes them straight through.
// Re-exported as `CoreSlotProps` so issue 0084 can keep the same binding when
// it replaces the internals.
export type CoreSlotProps = SphereCanvasProps;

export function CoreSlot(props: CoreSlotProps) {
  return (
    <>
      {/* `.core` carries the 300×300 depth footprint; SphereCanvas fills it as
          the placeholder orb. The canvas is absolute/inset:0 (hud.css), so the
          fixed-size `.core` box gives it a stable square to render into rather
          than the full window. */}
      <div className="core">
        <SphereCanvas {...props} />
      </div>
      <div className="core-label">CORE · conscience</div>
    </>
  );
}
