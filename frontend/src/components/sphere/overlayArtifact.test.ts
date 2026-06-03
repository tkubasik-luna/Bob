import { describe, expect, test } from "vitest";
import type { ComponentDescriptor } from "../../types/ws";
import { overlayChip, overlayRefMarker, overlaySpeechText } from "./overlayArtifact";

const md = (content: string): ComponentDescriptor => ({
  component: "Markdown",
  props: { content },
});
const mail = (over: Record<string, unknown> = {}): ComponentDescriptor => ({
  component: "Mail",
  props: {
    from: { name: "Marie", email: "marie@lunabee.com" },
    subject: "Q3 forecast",
    bodyPreview: "Deck for Thursday.",
    gmailWebUrl: "https://mail.google.com/x",
    ...over,
  },
});

describe("overlayChip", () => {
  test("single-type stacks map to the mockup chip label", () => {
    expect(overlayChip([md("a")])).toBe("FICHIER");
    expect(overlayChip([mail()])).toBe("BOÎTE");
  });

  test("a mixed-type stack falls back to SURFACE", () => {
    expect(overlayChip([md("a"), mail()])).toBe("SURFACE");
  });

  test("an unknown single type falls back to SURFACE", () => {
    expect(overlayChip([{ component: "Hologram", props: {} }])).toBe("SURFACE");
  });
});

describe("overlayRefMarker", () => {
  test("is a stable 4-char uppercase hex, deterministic per payload", () => {
    const a = overlayRefMarker([md("a"), mail()]);
    const b = overlayRefMarker([md("a"), mail()]);
    expect(a).toMatch(/^[0-9A-F]{4}$/);
    expect(a).toBe(b);
  });

  test("differs across differently-shaped stacks", () => {
    expect(overlayRefMarker([md("a")])).not.toBe(overlayRefMarker([md("a"), md("b")]));
  });
});

describe("overlaySpeechText", () => {
  test("strips Markdown syntax so TTS reads clean prose", () => {
    const text = overlaySpeechText([md("# Heading\n\n- **bold** item\n\n`code`")]);
    expect(text).toContain("Heading");
    expect(text).toContain("bold item");
    expect(text).toContain("code");
    expect(text).not.toContain("#");
    expect(text).not.toContain("**");
    expect(text).not.toContain("`");
  });

  test("reads a mail as sender → subject → body", () => {
    const text = overlaySpeechText([mail()]);
    expect(text).toBe("Courriel de Marie. Q3 forecast Deck for Thursday.");
  });

  test("joins a composite stack with blank lines, skipping empty + unknown sections", () => {
    const text = overlaySpeechText([
      md("Alpha"),
      { component: "Hologram", props: { secret: "x" } },
      mail({ subject: "Beta", bodyPreview: "" }),
    ]);
    expect(text).toContain("Alpha");
    expect(text).toContain("Beta");
    expect(text).not.toContain("secret");
    expect(text).not.toContain("x");
  });

  test("an empty / content-less stack yields an empty string", () => {
    expect(overlaySpeechText([])).toBe("");
    expect(overlaySpeechText([md("")])).toBe("");
  });
});
