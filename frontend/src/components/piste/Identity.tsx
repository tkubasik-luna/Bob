// Identity.tsx — Piste 3D · Nacre top-left identity (PRD 0014 / issue 0083).
//
// `● BOB · {état}` mark + the tagline "nacre — sphère liquide · sanctuaire en
// profondeur", styled to match the mockup (`.piste-id` / `.id-*` in p3d.css).
// The {état} word reflects the CURRENT orb state in French, reusing the same
// derivation SphereUI uses for the orb: the production `useSphereState`
// (idle/think/speak/error) widened by the dev-tweaks `forcedState` override
// (which can also be listen/alert). Owned by this issue.

import { useSphereState } from "../../sphere/useSphereState";
import { type DevForcedSphereState, useDevTweaksStore } from "../../state/devTweaksStore";

/** Map the full sphere-state union onto the French state word shown after
 * `BOB ·`. Covers all six states the dev override can produce; the four
 * production states (idle/think/speak/error) are the ones reached without
 * `?dev=1`. Mirrors the mockup's BOB_STAT spirit (repos/réflexion/…). */
const STATE_WORD_FR: Record<DevForcedSphereState, string> = {
  idle: "repos",
  listen: "écoute",
  think: "réflexion",
  speak: "réponse",
  alert: "alerte",
  error: "erreur",
};

export function Identity() {
  const derivedState = useSphereState();
  const forcedState = useDevTweaksStore((s) => s.forcedState);
  // Same precedence as SphereUI's orb: a dev `forcedState` wins over the
  // production derivation so the identity word tracks whatever the orb shows.
  const effectiveState = forcedState ?? derivedState;
  const stateWord = STATE_WORD_FR[effectiveState];

  return (
    <div className="piste-id">
      <div className="id-mark">
        <span className="id-glyph" />
        <span className="id-name">BOB</span>
        <span className="id-state">· {stateWord}</span>
      </div>
      <div className="id-tagline">nacre — sphère liquide · sanctuaire en profondeur</div>
    </div>
  );
}
