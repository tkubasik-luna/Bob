import { useCallback, useEffect, useState } from "react";
import { SphereUI } from "./components/SphereUI";
import { DebugView } from "./components/debug/DebugView";
import { SETUP_COMPLETE_KEY, SetupScreen } from "./components/setup/SetupScreen";
import { fetchLlmSelection, pingLm } from "./lib/llmApi";

type UiMode = "new" | "debug";

function getUiMode(): UiMode {
  if (typeof window === "undefined") return "new";
  const ui = new URLSearchParams(window.location.search).get("ui");
  if (ui === "debug") return "debug";
  return "new";
}

/** App phase for the HUD (`new`) window. The model/provider SetupScreen gates
 * entry to the main Sphere HUD so the user picks (and loads) a model up front
 * instead of landing on a misconfigured backend. `loading` covers the initial
 * "should we skip setup?" probe so the HUD never flashes before the decision. */
type Phase = "loading" | "setup" | "ready";

/** Decide the initial phase: skip setup only when the user completed it before
 * AND the configured backend is actually usable now (Claude CLI, or a
 * reachable LM Studio server with a pinned model). A previously-fine server
 * that is now down re-shows setup rather than booting into a dead HUD. */
async function resolveInitialPhase(): Promise<Phase> {
  if (window.localStorage.getItem(SETUP_COMPLETE_KEY) !== "1") return "setup";
  try {
    const sel = await fetchLlmSelection();
    if (sel.provider === "claude_cli") return "ready";
    if (!sel.lm_model) return "setup";
    const ping = await pingLm(sel.base_url ?? undefined);
    return ping.reachable ? "ready" : "setup";
  } catch {
    // Backend unreachable / selection endpoint failing → let the user reconfigure.
    return "setup";
  }
}

export default function App() {
  const mode = getUiMode();
  const [phase, setPhase] = useState<Phase>("loading");

  useEffect(() => {
    if (mode !== "new") return;
    let cancelled = false;
    void resolveInitialPhase().then((next) => {
      if (!cancelled) setPhase(next);
    });
    return () => {
      cancelled = true;
    };
  }, [mode]);

  const enterHud = useCallback(() => setPhase("ready"), []);

  if (mode === "debug") return <DebugView />;
  if (phase === "loading") return null;
  if (phase === "setup") return <SetupScreen onReady={enterHud} />;
  return <SphereUI />;
}
