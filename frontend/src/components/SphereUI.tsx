import { SphereCanvas } from "../sphere/SphereCanvas";
import { useSphereState } from "../sphere/useSphereState";
import { HudTasks } from "./sphere/HudTasks";

// V1 locked props per PRD 0004: warm + calm + liquid mercury.
// motion/glow defaults match the mockup TWEAK_DEFAULTS.
// The high-level sphere state is derived from the chat store via
// `useSphereState` (issue #0029) and passed to both the canvas (drives the
// shader crossfade) and the wrapper `.app` class (drives CSS var retinting).
export function SphereUI() {
  const sphereState = useSphereState();
  return (
    <div className={`app theme-warm mood-calm state-${sphereState} surface-none`}>
      <SphereCanvas
        state={sphereState}
        variant={0}
        motion={0.55}
        glow={0.7}
        theme="warm"
        mood="calm"
      />
      <div className="hud-zone tr">
        <HudTasks />
      </div>
    </div>
  );
}
