import { useCallback, useState } from "react";
import { SphereUI } from "./components/SphereUI";
import { DebugView } from "./components/debug/DebugView";
import { SetupScreen } from "./components/setup/SetupScreen";

type UiMode = "new" | "debug";

function getUiMode(): UiMode {
  if (typeof window === "undefined") return "new";
  const ui = new URLSearchParams(window.location.search).get("ui");
  if (ui === "debug") return "debug";
  return "new";
}

/** App phase for the HUD (`new`) window. The per-role SetupScreen ALWAYS gates
 * entry to the main Sphere HUD: every launch starts on it (seeded from the
 * current committed map, so passing through is one Démarrer click) and the
 * models are explicitly (re)loaded before the HUD mounts — never a HUD landing
 * on a misconfigured or half-loaded backend. */
type Phase = "setup" | "ready";

export default function App() {
  const mode = getUiMode();
  const [phase, setPhase] = useState<Phase>("setup");

  const enterHud = useCallback(() => setPhase("ready"), []);

  if (mode === "debug") return <DebugView />;
  if (phase === "setup") return <SetupScreen onReady={enterHud} />;
  return <SphereUI />;
}
