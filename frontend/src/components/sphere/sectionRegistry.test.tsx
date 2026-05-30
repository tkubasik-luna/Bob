import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { MarkdownSection } from "./MarkdownSection";
import { NotImplementedSection } from "./NotImplementedSection";
import { sectionRegistry } from "./sectionRegistry";

describe("sectionRegistry", () => {
  test("maps the MVP `Markdown` entry to the MarkdownSection renderer (non-structured)", () => {
    const entry = sectionRegistry.Markdown;
    expect(entry).toBeDefined();
    expect(entry.Component).toBe(MarkdownSection);
    // The `structured` flag drives auto-open; Markdown is text → false.
    expect(entry.structured).toBe(false);
  });

  test("an unknown component name is absent from the registry", () => {
    expect(sectionRegistry.Hologram).toBeUndefined();
  });
});

describe("MarkdownSection", () => {
  test("renders its `props.content` as Markdown via the `.ov-md` article", () => {
    const { container } = render(<MarkdownSection props={{ content: "# Title\n\n- a\n- b" }} />);
    const article = container.querySelector(".ov-md");
    expect(article).not.toBeNull();
    expect(article?.querySelector(".md-h1")?.textContent).toBe("Title");
    expect(article?.querySelectorAll(".md-ul li")).toHaveLength(2);
  });

  test("a non-string / missing content renders an empty article rather than crashing", () => {
    const { container } = render(<MarkdownSection props={{ content: 42 }} />);
    expect(container.querySelector(".ov-md")).not.toBeNull();
  });
});

describe("NotImplementedSection", () => {
  test("shows the component name and the hint, with no raw props", () => {
    const { container, getByText } = render(<NotImplementedSection name="Hologram" />);
    expect(container.querySelector(".ov-section-unsupported")).not.toBeNull();
    expect(getByText("Hologram")).toBeDefined();
    expect(container.textContent).toContain("Section non supportée");
  });
});
