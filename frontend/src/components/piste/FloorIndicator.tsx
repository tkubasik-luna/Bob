// FloorIndicator.tsx — Piste 3D · Nacre voice-floor indicator (PRD 0016
// Annexe A.2 / issue 0108).
//
// A discreet pill that reflects WHO HAS THE FLOOR in a full-duplex voice turn:
// idle / listening (user) / thinking / speaking (Bob). The state is driven
// purely by the live `turn_state` voice events from `/ws/debug` via
// {@link useTurnState} — mounting this component is the whole wiring.
//
// It is voice-specific and orthogonal to the orb's `useSphereState` (which the
// text/chat path also drives): this pill only animates during a real voice
// turn. While idle it stays muted so it adds no chrome during text-only use.
// Co-located styles live in `FloorIndicator.css`.

import { type FloorState, useTurnState } from "../../hooks/useTurnState";
import "./FloorIndicator.css";

/** Per-state presentation: a French label + a CSS state class. The four states
 * mirror the `TurnFsm` (`bob.turn_fsm`); `idle` is the resting floor. */
const FLOOR_LABEL: Record<FloorState, string> = {
  idle: "veille",
  user_speaking: "écoute",
  thinking: "réflexion",
  bob_speaking: "réponse",
};

export function FloorIndicator() {
  const floor = useTurnState();
  const active = floor !== "idle";

  return (
    <output
      className={`floor-indicator floor-${floor} ${active ? "is-active" : ""}`}
      data-testid="floor-indicator"
      data-floor={floor}
      aria-label={`Tour de parole : ${FLOOR_LABEL[floor]}`}
    >
      <span className="floor-dot" aria-hidden="true" />
      <span className="floor-label">{FLOOR_LABEL[floor]}</span>
    </output>
  );
}
