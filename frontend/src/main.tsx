import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import "./styles/hud.css";
// PRD 0014 — the Piste 3D · Nacre HUD shell. Loaded AFTER hud.css so the
// `.piste`-scoped foundation rules win where they intentionally override the
// legacy HUD palette/positioning for the `?ui=new` window only.
import "./styles/p3d.css";
import App from "./App.tsx";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
