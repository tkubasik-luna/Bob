/**
 * Minimal regex-based JSON syntax highlighter for debug payloads. Pulled out
 * of `DebugRow` in slice 0044 so the new `LlmCallNode` can reuse it for the
 * `messages` / `response` panels without duplicating the tokenizer.
 *
 * We avoid pulling in a dependency (`react-json-view`, prism, …) because the
 * payload structure is fully under our control and a 4-token highlighter is
 * enough to make the dump readable. Tokens: object keys (key color),
 * strings, numbers, booleans+null. Anything else (commas, braces, whitespace)
 * inherits the parent text color.
 *
 * Memoized on the input string so collapsing/expanding the row doesn't
 * re-tokenize a 30-message LLM dump.
 */

import { type CSSProperties, memo, useMemo } from "react";

export const HighlightedJson = memo(function HighlightedJson({ json }: { json: string }) {
  const tokens = useMemo(() => tokenizeJson(json), [json]);
  return (
    <pre
      style={{
        margin: "4px 0 0 0",
        padding: "8px 10px",
        background: "rgba(0, 0, 0, 0.32)",
        borderRadius: "3px",
        overflowX: "auto",
        whiteSpace: "pre",
        fontFamily: "inherit",
        fontSize: "11px",
        lineHeight: "1.45",
        color: "rgba(223, 239, 255, 0.88)",
      }}
    >
      {tokens.map((tok, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: token slices are positional, index is the natural identity
        <span key={i} style={tok.style}>
          {tok.text}
        </span>
      ))}
    </pre>
  );
});

type JsonToken = { text: string; style: CSSProperties };

const TOKEN_STYLE = {
  key: { color: "#7dd3fc" }, // cyan-300
  string: { color: "#bef264" }, // lime-300
  number: { color: "#fcd34d" }, // amber-300
  literal: { color: "#f0abfc" }, // fuchsia-300
  plain: {} as CSSProperties,
} as const satisfies Record<string, CSSProperties>;

/**
 * Single regex with named alternatives — runs in O(n) on the pretty-printed
 * JSON. The `key` arm only matches when the string is immediately followed
 * by `:` so that string *values* don't get colored like keys.
 */
const JSON_TOKEN_RE =
  /"(?:\\.|[^"\\])*"(?:\s*:)?|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\btrue\b|\bfalse\b|\bnull\b/g;

function tokenizeJson(json: string): JsonToken[] {
  const out: JsonToken[] = [];
  let lastIndex = 0;
  for (const match of json.matchAll(JSON_TOKEN_RE)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      out.push({ text: json.slice(lastIndex, start), style: TOKEN_STYLE.plain });
    }
    const raw = match[0];
    if (raw.startsWith('"')) {
      if (raw.endsWith(":") || raw.match(/"\s*:$/)) {
        const colonIdx = raw.lastIndexOf(":");
        const stringPart = raw.slice(0, colonIdx).trimEnd();
        const between = raw.slice(stringPart.length, colonIdx);
        out.push({ text: stringPart, style: TOKEN_STYLE.key });
        if (between.length > 0) out.push({ text: between, style: TOKEN_STYLE.plain });
        out.push({ text: ":", style: TOKEN_STYLE.plain });
      } else {
        out.push({ text: raw, style: TOKEN_STYLE.string });
      }
    } else if (raw === "true" || raw === "false" || raw === "null") {
      out.push({ text: raw, style: TOKEN_STYLE.literal });
    } else {
      out.push({ text: raw, style: TOKEN_STYLE.number });
    }
    lastIndex = start + raw.length;
  }
  if (lastIndex < json.length) {
    out.push({ text: json.slice(lastIndex), style: TOKEN_STYLE.plain });
  }
  return out;
}
