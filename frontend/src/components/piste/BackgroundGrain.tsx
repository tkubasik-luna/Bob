// BackgroundGrain.tsx — Piste 3D · Nacre background (PRD 0014 / issue 0083).
//
// The two non-interactive fond layers behind the 3D stage: `.piste-bg` paints
// the nacre radial/linear gradient wash over the `#160F18` base, `.piste-grain`
// overlays a fine film grain. Both are pointer-events:none and live at the
// bottom of the piste stacking order (see `styles/p3d.css`). Owned by this
// issue; downstream issues never touch it.
export function BackgroundGrain() {
  return (
    <>
      <div className="piste-bg" />
      <div className="piste-grain" />
    </>
  );
}
