import { ChatView } from "./components/ChatView";
import { SphereUI } from "./components/SphereUI";
import { DebugView } from "./components/debug/DebugView";

type UiMode = "legacy" | "new" | "debug";

function getUiMode(): UiMode {
  if (typeof window === "undefined") return "legacy";
  const ui = new URLSearchParams(window.location.search).get("ui");
  if (ui === "new") return "new";
  if (ui === "debug") return "debug";
  return "legacy";
}

export default function App() {
  const mode = getUiMode();
  if (mode === "debug") return <DebugView />;
  if (mode === "new") return <SphereUI />;
  return <ChatView />;
}
