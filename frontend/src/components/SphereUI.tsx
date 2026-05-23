export function SphereUI() {
  return (
    <div className="app theme-warm mood-calm state-idle surface-none">
      <div
        className="hud-zone"
        style={{
          inset: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "12px",
        }}
      >
        <span className="text-accent font-mono" style={{ letterSpacing: "0.28em" }}>
          SPHERE UI
        </span>
        <span
          className="font-mono"
          style={{ color: "var(--ink-faint)", fontSize: "10px", letterSpacing: "0.18em" }}
        >
          UNDER CONSTRUCTION
        </span>
      </div>
    </div>
  );
}
