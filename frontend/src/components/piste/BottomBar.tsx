// BottomBar.tsx — Piste 3D · Nacre bottom zone (PRD 0014 / issue 0083).
//
// Hosts the EXISTING transcript line + input field + mute toggle so they stay
// functional while the shell lands (provisional placement). Issue 0090 reskins
// and repositions these to the « nacre » input bar. The transcript and input
// keep their hud.css positioning via the legacy `.hud-zone.b` wrapper; the mute
// toggle is already a self-positioned fixed element (bottom-right, hud.css), so
// it's rendered as a sibling rather than nested in the bottom zone.

import type { SphereDerivedState } from "../../sphere/useSphereState";
import { InputField } from "../sphere/InputField";
import { MuteToggle } from "../sphere/MuteToggle";
import { TranscriptLine } from "../sphere/TranscriptLine";

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
    <>
      <div className="hud-zone b">
        <TranscriptLine state={transcriptState} hidden={overlayOpen} />
        <InputField />
      </div>
      <MuteToggle />
    </>
  );
}
