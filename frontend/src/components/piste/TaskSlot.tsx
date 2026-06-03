// TaskSlot.tsx — Piste 3D · Nacre left slot (PRD 0014 / issues 0083, 0085).
//
// The left slot of the 3D stage (tilted +24° into depth by `.layout-depth
// .slot-task`). Rendered inside `<div className="slot-task">` by SphereUI, so
// the depth positioning already applies — this component owns only the slot
// CONTENTS and takes no props from the shell.
//
// Issue 0085 fills it with the BOB card — the front card of the (future) thread
// deck — bound to the live orchestrator turn (prompt → réflexion → tâches en
// arrière-plan → réponse → perf). `BobCard` renders nothing until there is a
// thread (no prompt / activity / answer), so the idle scene stays empty exactly
// like the mockup. Issue 0086 wraps a multi-card deck (sub-task cards +
// stacking) AROUND this card — at which point this slot renders the deck and
// the BOB card becomes its front card.
import { BobCard } from "./BobCard";

export function TaskSlot() {
  return <BobCard />;
}
