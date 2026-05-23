/**
 * Pure heuristic that decides whether an assistant response should be displayed
 * in the markdown overlay (true) or kept inline in the transcript line (false).
 *
 * Returns true when EITHER:
 *   - the content has more than 3 lines, OR
 *   - the content matches at least one structural markdown pattern:
 *       - heading: `# ` ... `###### `
 *       - unordered list: `- ` / `* `
 *       - ordered list: `1. `, `42. `, ...
 *       - fenced code block: ` ``` `
 *       - blockquote: `> `
 *       - GFM table row: `| ... |`
 *       - inline link: `[label](href)`
 *       - thematic break: `---`
 *
 * Empty / whitespace-only content returns false.
 */

const STRUCTURAL_MARKDOWN_PATTERNS: ReadonlyArray<RegExp> = [
  /^#{1,6}\s/m, // heading
  /^\s*[-*]\s/m, // unordered list item
  /^\s*\d+\.\s/m, // ordered list item
  /```/, // fenced code block (opening or closing)
  /^\s*>\s/m, // blockquote
  /\|.*\|/, // GFM table row
  /\[[^\]]+\]\([^)]+\)/, // inline link
  /^\s*---\s*$/m, // thematic break / hr
];

export function shouldOverlayResponse(content: string): boolean {
  if (content.length === 0) {
    return false;
  }

  if (content.split("\n").length > 3) {
    return true;
  }

  for (const pattern of STRUCTURAL_MARKDOWN_PATTERNS) {
    if (pattern.test(content)) {
      return true;
    }
  }

  return false;
}
