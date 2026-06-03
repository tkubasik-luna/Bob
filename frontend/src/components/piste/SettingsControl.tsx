// SettingsControl.tsx — Piste 3D · Nacre top-right gear zone (PRD 0014 / issue 0083).
//
// The top-right settings zone. For the foundation slice it renders the EXISTING
// `<ProviderPicker/>` inside a top-right container so LLM provider/model
// switching stays functional (provisional placement — the picker used to mount
// in the old top-left `.hud-zone.tl`, which the shell removed). Issue 0089
// replaces the internals with the real « Réglages » settings modal (the
// `.settings-*` block in the mockup) and deletes ProviderPicker.
//
// The container uses `.piste-settings-zone` (a thin, issue-owned positioning
// wrapper, styled inline here so it needs no foundation CSS) rather than the
// mockup's `.settings-zone` — that name belongs to 0089's modal port.

import { ProviderPicker } from "../sphere/ProviderPicker";

export function SettingsControl() {
  return (
    <div
      className="piste-settings-zone"
      // Provisional inline placement (top-right), kept off the foundation sheet
      // so 0089 can drop it wholesale. pointer-events re-enabled for the picker.
      style={{
        position: "absolute",
        top: 24,
        right: 26,
        zIndex: 60,
        pointerEvents: "auto",
      }}
    >
      <ProviderPicker />
    </div>
  );
}
