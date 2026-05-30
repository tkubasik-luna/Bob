import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { ComponentDescriptor } from "../../types/ws";
import { SectionsOverlay } from "./SectionsOverlay";

const MARKDOWN_SECTION: ComponentDescriptor = {
  component: "Markdown",
  props: { content: "# Big heading\n\n- one\n- two" },
};

describe("SectionsOverlay", () => {
  test("renders nothing when `sections === null`", () => {
    const { container } = render(<SectionsOverlay sections={null} onClose={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  test("renders nothing for an empty list", () => {
    const { container } = render(<SectionsOverlay sections={[]} onClose={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the single shell chrome (corner brackets / header / footer)", () => {
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={vi.fn()} />,
    );
    const card = container.querySelector(".overlay-card");
    expect(card).not.toBeNull();
    // Four corner brackets — TL/TR/BL/BR.
    expect(card?.querySelectorAll(".ov-corner")).toHaveLength(4);
    expect(card?.querySelector(".ov-header .ov-type-chip")?.textContent).toBe("SECTIONS");
    expect(card?.querySelector(".ov-header .ov-id-tag")?.textContent).toMatch(
      /^REF · SEC-[0-9A-F]{4}$/,
    );
    expect(card?.querySelector(".ov-footer")).not.toBeNull();
  });

  test("renders a Markdown section through the registry", () => {
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={vi.fn()} />,
    );
    const article = container.querySelector(".ov-section .ov-md");
    expect(article).not.toBeNull();
    expect(article?.querySelector(".md-h1")?.textContent).toBe("Big heading");
    expect(article?.querySelectorAll(".md-ul li")).toHaveLength(2);
  });

  test("renders multiple sections in list order inside one shell", () => {
    const sections: ComponentDescriptor[] = [
      { component: "Markdown", props: { content: "# First" } },
      { component: "Markdown", props: { content: "# Second" } },
    ];
    const { container } = render(<SectionsOverlay sections={sections} onClose={vi.fn()} />);
    // Exactly one shell …
    expect(container.querySelectorAll(".overlay-card")).toHaveLength(1);
    // … containing both sections in order.
    const headings = Array.from(container.querySelectorAll(".ov-section .md-h1")).map(
      (h) => h.textContent,
    );
    expect(headings).toEqual(["First", "Second"]);
  });

  test("an unknown component renders a NotImplemented card (name shown, no raw props, no crash)", () => {
    const sections: ComponentDescriptor[] = [
      { component: "Hologram", props: { secret: "should-not-render", payload: { big: "data" } } },
    ];
    const { container } = render(<SectionsOverlay sections={sections} onClose={vi.fn()} />);
    const card = container.querySelector(".ov-section-unsupported");
    expect(card).not.toBeNull();
    // The component name is shown …
    expect(card?.textContent).toContain("Hologram");
    expect(card?.textContent).toContain("Section non supportée");
    // … but the raw props never reach the DOM.
    expect(container.textContent).not.toContain("should-not-render");
    expect(container.textContent).not.toContain("big");
  });

  test("a known + unknown section coexist (known renders, unknown falls back)", () => {
    const sections: ComponentDescriptor[] = [
      MARKDOWN_SECTION,
      { component: "Hologram", props: {} },
    ];
    const { container } = render(<SectionsOverlay sections={sections} onClose={vi.fn()} />);
    expect(container.querySelector(".ov-md")).not.toBeNull();
    expect(container.querySelector(".ov-section-unsupported")).not.toBeNull();
  });

  test("the body wrapper carries the scrollable `.ov-body` so a long list scrolls", () => {
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={vi.fn()} />,
    );
    // The scroll affordance lives on `.ov-body` (overflow-y: auto in hud.css);
    // we assert the structural class is present so the long-list path is wired.
    expect(container.querySelector(".overlay-card .ov-body.ov-sections")).not.toBeNull();
  });

  test("Escape keydown calls `onClose`", () => {
    const onClose = vi.fn();
    render(<SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("does NOT register a global Escape listener when closed", () => {
    const onClose = vi.fn();
    render(<SectionsOverlay sections={null} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  test("clicking the `.overlay-stage` backdrop calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />,
    );
    const stage = container.querySelector<HTMLDivElement>(".overlay-stage");
    if (stage) fireEvent.click(stage);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking inside the `.overlay-card` does NOT call `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />,
    );
    const card = container.querySelector<HTMLDivElement>(".overlay-card");
    if (card) fireEvent.click(card);
    expect(onClose).not.toHaveBeenCalled();
  });

  test("clicking the header `×` button calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />,
    );
    const close = container.querySelector<HTMLButtonElement>(".ov-close");
    if (close) fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking the footer `DISMISS` action calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />,
    );
    const dismiss = container.querySelector<HTMLButtonElement>(
      '.ov-footer button[aria-label="dismiss"]',
    );
    if (dismiss) fireEvent.click(dismiss);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
