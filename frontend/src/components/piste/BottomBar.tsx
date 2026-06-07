// BottomBar.tsx — Piste 3D · Nacre bottom zone (PRD 0014 / issue 0090).
//
// Re-skins + re-positions the existing voice/text controls into the « Nacre »
// visual language, replacing the provisional `.hud-zone b` placement the
// foundation (issue 0083) shipped:
//   - InputField   → minimal, always-visible composer pinned bottom-CENTRE.
//   - TranscriptLine → the live voice caption, sitting just above the input
//                      (bottom-CENTRE); hides while a surface overlay is open.
// The voice/mute toggle no longer lives here — it moved into the « RÉGLAGES »
// panel (SettingsControl, top-right).
//
// The two leaves keep their behaviour, props and `hud-*` class names intact —
// the nacre look comes entirely from the co-located `BottomBar.css`, which
// scopes overrides under `.piste .p3d-bottom …` so they win over the legacy
// `hud.css` rules without touching the leaves (and so their tests stay green).
//
// Layout contract: the centre column is width-clamped and horizontally centred
// so it never reaches the side slots (task left, data right, both ~40px from
// the edge and vertically centred).
// The window's top drag strip (`.drag-region`, owned by SphereUI/p3d.css) is
// untouched — this bar lives at the bottom and never covers it.

import type { SphereDerivedState } from "../../sphere/useSphereState";
import { InputField } from "../sphere/InputField";
import { TranscriptLine } from "../sphere/TranscriptLine";
import "./BottomBar.css";

type BottomBarProps = {
  /** Narrowed sphere state driving the transcript's slot (hint/thinking/text).
   * SphereUI derives this from the effective sphere state. */
  transcriptState: SphereDerivedState;
  /** When a surface (SectionsOverlay) is open the transcript hides so the
   * surface alone carries the context — mirrors the legacy SphereUI behaviour
   * (`hidden={overlayOpen}`). */
  overlayOpen: boolean;
};

export function BottomBar({ transcriptState, overlayOpen }: BottomBarProps) {
  return (
    <div className="p3d-bottom">
      {/* Bottom-centre column: the live voice caption stacked above the
       * always-visible composer. Width-clamped so it stays clear of the
       * left/right slots. Only this column takes pointer events. */}
      <div className="p3d-bottom-center">
        <TranscriptLine state={transcriptState} hidden={overlayOpen} />
        <InputField />
      </div>
      {/* The voice/mute control now lives in the « RÉGLAGES » panel
       * (SettingsControl, top-right) — no bottom-right glyph here. */}
    </div>
  );
}
