import { ChatView } from "./components/ChatView";
import { SphereUI } from "./components/SphereUI";

function getUiMode(): "legacy" | "new" {
  if (typeof window === "undefined") return "legacy";
  const ui = new URLSearchParams(window.location.search).get("ui");
  return ui === "new" ? "new" : "legacy";
}

export default function App() {
  return getUiMode() === "new" ? <SphereUI /> : <ChatView />;
}
