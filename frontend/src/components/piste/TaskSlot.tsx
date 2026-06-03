// TaskSlot.tsx — Piste 3D · Nacre left slot (PRD 0014 / issue 0083).
//
// The left slot of the 3D stage (tilted +24° into depth by `.layout-depth
// .slot-task`). For the foundation slice this is an EMPTY placeholder: the
// Bob/sub-task thread deck fills it in issues 0085 (Bob card) and 0086
// (sub-task cards). Rendered inside `<div className="slot-task">` by SphereUI,
// so the depth positioning already applies — this component owns only the slot
// CONTENTS. Renders nothing for now (no faint marker, matching the idle mockup
// where the deck simply isn't present until there's a thread).
export function TaskSlot() {
  return null;
}
