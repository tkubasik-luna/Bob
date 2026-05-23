import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { MarkdownOverlay } from "./MarkdownOverlay";

const SAMPLE_MARKDOWN = `# Big heading

## Section two

Some text with [a link](https://example.com).

- first
- second
- third

\`\`\`ts
const a = 1;
\`\`\`

| Col A | Col B |
| ----- | ----- |
| a1    | b1    |
| a2    | b2    |

> A blockquote line
`;

describe("MarkdownOverlay", () => {
  test("renders nothing when `content === null`", () => {
    const onClose = vi.fn();
    const { container } = render(<MarkdownOverlay content={null} onClose={onClose} />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the mockup chrome (header / body / footer) when content is provided", () => {
    const onClose = vi.fn();
    const { container } = render(<MarkdownOverlay content="hello" onClose={onClose} />);
    expect(container.querySelector(".overlay-stage")).not.toBeNull();
    const card = container.querySelector(".overlay-card");
    expect(card).not.toBeNull();
    // Four corner brackets — TL/TR/BL/BR — mirror the mockup chrome.
    expect(card?.querySelectorAll(".ov-corner")).toHaveLength(4);
    expect(card?.querySelector(".ov-header .ov-source-tag")?.textContent).toBe("BOB · SURFACING");
    expect(card?.querySelector(".ov-header .ov-type-chip")?.textContent).toBe("MARKDOWN");
    expect(card?.querySelector(".ov-header .ov-id-tag")?.textContent).toMatch(
      /^REF · NTS-[0-9A-F]{4}$/,
    );
    expect(card?.querySelector(".ov-footer")).not.toBeNull();
    // Body wraps an `<article class="ov-md">` so the markdown CSS applies.
    expect(card?.querySelector(".ov-body .ov-md")).not.toBeNull();
  });

  test("renders markdown structure with the mockup `.md-*` classes (heading, list, code, table, blockquote, link)", () => {
    const onClose = vi.fn();
    const { container } = render(<MarkdownOverlay content={SAMPLE_MARKDOWN} onClose={onClose} />);
    const article = container.querySelector(".ov-md");
    expect(article).not.toBeNull();
    // Headings — h1 + h2 carry the mockup classes (the wrapping `.md-h` plus
    // their level-specific token).
    expect(article?.querySelector(".md-h1")?.textContent).toBe("Big heading");
    expect(article?.querySelector(".md-h2")?.textContent).toBe("Section two");
    // Unordered list with the three items.
    const ul = article?.querySelector(".md-ul");
    expect(ul).not.toBeNull();
    expect(ul?.querySelectorAll("li")).toHaveLength(3);
    // Fenced code block — outer `<pre class="md-pre">` is the styling anchor.
    expect(article?.querySelector(".md-pre")).not.toBeNull();
    // Blockquote uses the mockup `.md-quote` class.
    expect(article?.querySelector(".md-quote")?.textContent?.trim()).toBe("A blockquote line");
    // Inline link → `.md-link`, opens in a new tab.
    const link = article?.querySelector("a.md-link");
    expect(link).not.toBeNull();
    expect(link?.getAttribute("href")).toBe("https://example.com");
    expect(link?.getAttribute("target")).toBe("_blank");
    // GFM table → real <th>/<td> rows so the `.ov-md` table styling applies.
    const table = article?.querySelector("table");
    expect(table).not.toBeNull();
    expect(table?.querySelectorAll("th")).toHaveLength(2);
    expect(table?.querySelectorAll("td").length).toBeGreaterThanOrEqual(4);
  });

  test("Escape keydown calls `onClose`", () => {
    const onClose = vi.fn();
    render(<MarkdownOverlay content="hello" onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("does NOT register a global Escape listener when closed (`content === null`)", () => {
    const onClose = vi.fn();
    render(<MarkdownOverlay content={null} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  test("clicking the `.overlay-stage` backdrop calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MarkdownOverlay content="hello" onClose={onClose} />);
    const stage = container.querySelector<HTMLDivElement>(".overlay-stage");
    expect(stage).not.toBeNull();
    if (stage) fireEvent.click(stage);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking inside the `.overlay-card` does NOT call `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MarkdownOverlay content="hello" onClose={onClose} />);
    const card = container.querySelector<HTMLDivElement>(".overlay-card");
    expect(card).not.toBeNull();
    if (card) fireEvent.click(card);
    expect(onClose).not.toHaveBeenCalled();
  });

  test("clicking the header `×` button calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MarkdownOverlay content="hello" onClose={onClose} />);
    const close = container.querySelector<HTMLButtonElement>(".ov-close");
    expect(close).not.toBeNull();
    if (close) fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking the footer `DISMISS` action calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MarkdownOverlay content="hello" onClose={onClose} />);
    // The footer DISMISS button is the third `.ov-action` and carries
    // `aria-label="dismiss"`. We pick it by aria-label so a reordering of
    // the buttons would surface here rather than silently break the test.
    const dismiss = container.querySelector<HTMLButtonElement>(
      '.ov-footer button[aria-label="dismiss"]',
    );
    expect(dismiss).not.toBeNull();
    if (dismiss) fireEvent.click(dismiss);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("body re-keys on content change so the CSS fade-in retriggers in place", () => {
    const onClose = vi.fn();
    const { container, rerender } = render(<MarkdownOverlay content="# first" onClose={onClose} />);
    const firstBody = container.querySelector(".ov-body");
    expect(firstBody).not.toBeNull();
    rerender(<MarkdownOverlay content="# second" onClose={onClose} />);
    const secondBody = container.querySelector(".ov-body");
    expect(secondBody).not.toBeNull();
    // The body wrapper's `key` is derived from the content hash, so React
    // mounts a brand-new node when the content changes — equivalent to a
    // remount of the body slot while the header / footer chrome remains.
    expect(secondBody).not.toBe(firstBody);
  });

  test("the REF marker is stable for a given content payload (no random per-render)", () => {
    const onClose = vi.fn();
    const { container, rerender } = render(
      <MarkdownOverlay content="stable payload" onClose={onClose} />,
    );
    const first = container.querySelector(".ov-id-tag")?.textContent;
    rerender(<MarkdownOverlay content="stable payload" onClose={onClose} />);
    const second = container.querySelector(".ov-id-tag")?.textContent;
    expect(first).toBe(second);
  });
});
