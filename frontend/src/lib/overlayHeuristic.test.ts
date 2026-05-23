import { describe, expect, test } from "vitest";
import { shouldOverlayResponse } from "./overlayHeuristic";

type Case = {
  name: string;
  content: string;
  expected: boolean;
};

const cases: Case[] = [
  // --- Negatives: plain short content that must stay inline. ---
  {
    name: "plain 1 line",
    content: "Il est 14:32.",
    expected: false,
  },
  {
    name: "plain 2 lines",
    content: "Salut.\nÇa va ?",
    expected: false,
  },
  {
    name: "plain 3 lines (boundary, should stay inline)",
    content: "Une ligne.\nDeux lignes.\nTrois lignes.",
    expected: false,
  },
  {
    name: "empty string",
    content: "",
    expected: false,
  },
  {
    name: "whitespace only",
    content: "   \n  \n  ",
    expected: false,
  },

  // --- Line-count trigger. ---
  {
    name: "plain 4 lines triggers via line count",
    content: "Un.\nDeux.\nTrois.\nQuatre.",
    expected: true,
  },

  // --- Headings. ---
  {
    name: "heading h1",
    content: "# Titre",
    expected: true,
  },
  {
    name: "heading h2",
    content: "## Sous-titre",
    expected: true,
  },
  {
    name: "heading h3",
    content: "### Encore plus petit",
    expected: true,
  },

  // --- Lists. ---
  {
    name: "unordered list with dash",
    content: "- premier item",
    expected: true,
  },
  {
    name: "unordered list with star",
    content: "* premier item",
    expected: true,
  },
  {
    name: "ordered list",
    content: "1. premier item",
    expected: true,
  },

  // --- Code fence. ---
  {
    name: "code fence triple backtick",
    content: "```\nconst x = 1;\n```",
    expected: true,
  },

  // --- Blockquote. ---
  {
    name: "blockquote",
    content: "> citation",
    expected: true,
  },

  // --- GFM table. ---
  {
    name: "GFM table",
    content: "| a | b |\n| --- | --- |\n| 1 | 2 |",
    expected: true,
  },

  // --- Inline link. ---
  {
    name: "inline link",
    content: "Voir [docs](https://example.com) pour plus.",
    expected: true,
  },

  // --- Horizontal rule. ---
  {
    name: "horizontal rule",
    content: "Avant\n---\nAprès",
    expected: true,
  },

  // --- Mix. ---
  {
    name: "mix heading + list",
    content: "## Titre\n- item",
    expected: true,
  },
  {
    name: "single line list still triggers",
    content: "- juste un item court",
    expected: true,
  },
];

describe("shouldOverlayResponse", () => {
  test.each(cases)("$name -> $expected", ({ content, expected }) => {
    expect(shouldOverlayResponse(content)).toBe(expected);
  });

  test("at least 3 negative cases are covered", () => {
    const negatives = cases.filter((c) => c.expected === false);
    expect(negatives.length).toBeGreaterThanOrEqual(3);
  });

  test("at least 12 distinct cases are covered", () => {
    expect(cases.length).toBeGreaterThanOrEqual(12);
  });
});
