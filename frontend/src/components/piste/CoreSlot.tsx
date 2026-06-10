// CoreSlot.tsx — Piste 3D · Nacre centre slot (PRD 0014 / issue 0084).
//
// The centre slot holds the conscience orb. Issue 0083 rendered the legacy
// `<SphereCanvas/>` here as a PLACEHOLDER; this slice swaps the internals for
// the ported conscience NEBULA orb (`orb/ConscienceOrb` — WebGL form 3, nacre
// rose/lavender). The orb stands alone — no label in front of it.
//
// Prop surface is UNCHANGED — `CoreSlotProps = SphereCanvasProps` — so the
// shell binding in `SphereUI` (`<CoreSlot {...orbProps} />`) still type-checks.
// We accept all the legacy orb props but only the tuning ones are forwarded to
// the nebula (`motion` / `glow` from `devTweaksStore`, `audioLevelRef` for the
// live TTS pulse). The `state` prop SphereUI passes is `forcedState ??
// derivedState`; we IGNORE its derived half and instead derive the orb's mood
// ourselves from the REAL stores via the pure `deriveOrbState` reducer — but we
// still honour a `?dev` FORCED state (read straight from `devTweaksStore`) so
// `DevControls` keeps exercising every mood. `variant` / `mood` / `theme` are
// part of the prop surface for compatibility but don't apply to the nebula
// (which has its own nacre palette + form); they're intentionally unused here.
//
// The orb mood drives the `--energy` CSS var on `.core` (reserved for glow
// intensity) via the reducer's `energy` output. There is no backdrop disc
// behind the orb — the canvas is transparent and stands alone.

import type { FloorState } from "../../hooks/useTurnState";
import { type OrbState, deriveOrbState } from "../../lib/orbState";
import type { SphereCanvasProps } from "../../sphere/SphereCanvas";
import { useDevTweaksStore } from "../../state/devTweaksStore";
import { useChatStore } from "../../store/chatStore";
import "./CoreSlot.css";
import { ConscienceOrb } from "./orb/ConscienceOrb";

// The orb prop surface for the core slot is SphereCanvas's prop surface plus
// the lifted voice floor — SphereUI derives these once and passes them straight
// through. `floor` feeds the reducer's voice-aware « écoute » rule (the wake
// word « Yo Bob » opens a `user_speaking` turn); optional so existing mounts
// without a floor keep the prior derivation.
export type CoreSlotProps = SphereCanvasProps & { floor?: FloorState };

/** The dev-forced state union (`devTweaksStore`) is a superset of `OrbState`
 * (same six names), so a forced value maps straight through. */
function forcedToOrbState(forced: OrbState | null): OrbState | null {
  return forced;
}

export function CoreSlot(props: CoreSlotProps): JSX.Element {
  // Tuning comes from the dev-tweaks store (so `?dev` + `DevControls` keep
  // working) rather than the passed props — same source SphereUI reads from, so
  // values are identical, but reading here keeps the orb self-contained.
  const motion = useDevTweaksStore((s) => s.motion);
  const glow = useDevTweaksStore((s) => s.glow);
  const forcedState = useDevTweaksStore((s) => s.forcedState);

  // Select each input the reducer needs SEPARATELY so every selector returns a
  // stable value (a primitive, or the `tasks` map reference zustand only swaps
  // on a task upsert — same pattern as `TaskSidebar`). Computing the reducer
  // INSIDE one selector would return a fresh `{state, energy}` object every
  // render and spin zustand into an infinite re-render loop. We derive in the
  // render body instead, which is cheap + pure.
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const isWaitingResponse = useChatStore((s) => s.isWaitingResponse);
  const speakingMsgId = useChatStore((s) => s.speakingMsgId);
  const isStreamingResponse = useChatStore((s) => s.streamingAssistant !== null);
  const tasks = useChatStore((s) => s.tasks);

  const derived = deriveOrbState(
    { connectionStatus, isWaitingResponse, speakingMsgId, isStreamingResponse },
    tasks,
    props.floor ?? "idle",
  );

  // A `?dev` forced state (if any) overrides the live derivation, exactly like
  // the legacy `forcedState ?? derivedState` precedence in SphereUI.
  const forced = forcedToOrbState(forcedState);
  const state = forced ?? derived.state;
  const energy = derived.energy;

  // The audio tap is owned by SphereUI (a single `useAudioLevel` ref shared
  // across the tree); we read it from the passed props so we don't open a
  // second analyser. Pulled out separately so the JSX stays tidy. (This is the
  // one prop we forward; the rest of `CoreSlotProps` is accepted for the shell
  // binding contract but intentionally unused — see the file header.)
  const audioLevelRef = props.audioLevelRef;

  // `.core` carries the 300×300 depth footprint and the `--energy` var
  // (reserved for glow intensity). The nebula canvas fills it — no backdrop disc
  // behind the orb (removed per design: no "fond") and no label in front of it.
  return (
    <div className="core core-nebula" style={{ "--energy": energy } as React.CSSProperties}>
      <ConscienceOrb state={state} motion={motion} glow={glow} audioLevelRef={audioLevelRef} />
    </div>
  );
}
