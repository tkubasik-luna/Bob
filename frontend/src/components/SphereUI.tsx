import { useChatWsBridge } from "../hooks/useChatWsBridge";
import { SphereCanvas } from "../sphere/SphereCanvas";
import { useSphereState } from "../sphere/useSphereState";
import { HudTasks } from "./sphere/HudTasks";
import { InputField } from "./sphere/InputField";
import { TranscriptLine } from "./sphere/TranscriptLine";
import { SphereWsContext } from "./sphere/sphereWsContext";

// V1 locked props per PRD 0004: warm + calm + liquid mercury.
// motion/glow defaults match the mockup TWEAK_DEFAULTS.
// The high-level sphere state is derived from the chat store via
// `useSphereState` (issue #0029) and passed to both the canvas (drives the
// shader crossfade) and the wrapper `.app` class (drives CSS var retinting).
//
// The WS connection lives at the top of the `?ui=new` tree (issue #0030
// follow-up): `useChatWsBridge` owns the single socket, dispatches every
// incoming `ServerMessage` into the store, and exposes `send` to the input
// field via React Context so the leaf doesn't open a second connection.
export function SphereUI() {
  const sphereState = useSphereState();
  const { send } = useChatWsBridge();
  return (
    <SphereWsContext.Provider value={send}>
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
        <div className="hud-zone b">
          <TranscriptLine state={sphereState} />
          <InputField />
        </div>
      </div>
    </SphereWsContext.Provider>
  );
}
