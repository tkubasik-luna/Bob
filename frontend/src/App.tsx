import { SphereUI } from "./components/SphereUI";
import { DebugView } from "./components/debug/DebugView";

type UiMode = "new" | "debug";

function getUiMode(): UiMode {
  if (typeof window === "undefined") return "new";
  const ui = new URLSearchParams(window.location.search).get("ui");
  if (ui === "debug") return "debug";
  return "new";
}

export default function App() {
  const mode = getUiMode();
  if (mode === "debug") return <DebugView />;
  return <SphereUI />;
}
