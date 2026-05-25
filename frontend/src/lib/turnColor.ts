/**
 * Deterministic color helpers for the per-`turn_id` chip in the debug feed.
 *
 * The goal is purely visual grouping: two events sharing the same `turn_id`
 * MUST receive the same chip color; two distinct `turn_id`s SHOULD (with very
 * high probability) get different hues so the operator's eye can pivot from
 * one chip to the next across a busy feed. We don't need cryptographic
 * spread — a cheap stable hash → HSL hue is plenty.
 *
 * Saturation and lightness are pinned so every hue lands in a readable range
 * against the dark `--bg` background. Pure red / pure blue would otherwise be
 * far brighter than e.g. yellow-greens at the same lightness, and that
 * fights the "perceptual stability across turns" goal.
 *
 * PRD: prd/0005-debug-view.md — slice: issues/0041-debug-view-row-expand.md
 */

/** Saturation used for every turn chip, in percent (0..100). */
const TURN_CHIP_SATURATION_PCT = 65;
/** Lightness used for every turn chip, in percent (0..100). */
const TURN_CHIP_LIGHTNESS_PCT = 55;

/** Number of hex chars displayed on the chip body (first N of the turn_id). */
export const TURN_CHIP_SHORT_LENGTH = 6;

/**
 * djb2-style multiplicative string hash. Cheap, well-distributed for short
 * UUID-like strings, and deterministic. Returns a non-negative 32-bit int.
 */
function hashString(input: string): number {
  let h = 5381;
  for (let i = 0; i < input.length; i += 1) {
    // (h * 33) ^ char — classic djb2 xor variant.
    h = ((h << 5) + h) ^ input.charCodeAt(i);
  }
  // `>>> 0` coerces the signed 32-bit Int back into an unsigned int.
  return h >>> 0;
}

/**
 * Stable hue in `[0, 360)` for a given `turn_id` string. Same input → same
 * hue; two distinct inputs are very likely to land on different hues for
 * realistic UUID strings.
 */
export function turnIdHue(turnId: string): number {
  return hashString(turnId) % 360;
}

/**
 * CSS `hsl(...)` color usable directly in `style.color` / `style.background`.
 * Pinned saturation + lightness keep every hue at comparable perceived
 * brightness against the dark debug background.
 */
export function turnIdColor(turnId: string): string {
  return `hsl(${turnIdHue(turnId)}, ${TURN_CHIP_SATURATION_PCT}%, ${TURN_CHIP_LIGHTNESS_PCT}%)`;
}

/**
 * Translucent background variant used by the "highlight all rows sharing
 * this turn_id" overlay. Lower alpha than the chip itself so the row text
 * stays legible.
 */
export function turnIdHighlightBg(turnId: string): string {
  return `hsla(${turnIdHue(turnId)}, ${TURN_CHIP_SATURATION_PCT}%, ${TURN_CHIP_LIGHTNESS_PCT}%, 0.18)`;
}

/**
 * Outline color used by the highlight overlay — same hue, a little brighter
 * than the background so the row "pops" without competing with the chip.
 */
export function turnIdHighlightOutline(turnId: string): string {
  return `hsla(${turnIdHue(turnId)}, ${TURN_CHIP_SATURATION_PCT}%, ${TURN_CHIP_LIGHTNESS_PCT}%, 0.65)`;
}

/**
 * First `TURN_CHIP_SHORT_LENGTH` chars of a UUID-like turn_id, used as the
 * visible chip body. Returns the full string if it's shorter than the cap.
 */
export function shortTurnId(turnId: string): string {
  return turnId.slice(0, TURN_CHIP_SHORT_LENGTH);
}
