import { SphereCanvas } from "../sphere/SphereCanvas";
import { HudTasks } from "./sphere/HudTasks";

// V1 locked props per PRD 0004: warm + calm + liquid mercury.
// motion/glow defaults match the mockup TWEAK_DEFAULTS.
export function SphereUI() {
  return (
    <div className="app theme-warm mood-calm state-idle surface-none">
      <SphereCanvas state="idle" variant={0} motion={0.55} glow={0.7} theme="warm" mood="calm" />
      <div className="hud-zone tr">
        <HudTasks />
      </div>
    </div>
  );
}
