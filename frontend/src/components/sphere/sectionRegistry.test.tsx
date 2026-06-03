import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { DocSurface } from "./DocSurface";
import { MailCard } from "./MailCard";
import { MarkdownSection } from "./MarkdownSection";
import { NotImplementedSection } from "./NotImplementedSection";
import { sectionRegistry } from "./sectionRegistry";

describe("sectionRegistry", () => {
  test("maps the `Markdown` entry to the DocSurface renderer (Document surface, non-structured)", () => {
    const entry = sectionRegistry.Markdown;
    expect(entry).toBeDefined();
    // Markdown now renders through the Document surface (mockup DocSurface),
    // which reuses the existing react-markdown renderer for its body.
    expect(entry.Component).toBe(DocSurface);
    expect(entry.structured).toBe(false);
  });

  test("maps `Mail` to the MailCard renderer as a structured section (issue 0067)", () => {
    const entry = sectionRegistry.Mail;
    expect(entry).toBeDefined();
    expect(entry.Component).toBe(MailCard);
    expect(entry.structured).toBe(true);
  });

  test("an unknown component name is absent from the registry", () => {
    expect(sectionRegistry.Hologram).toBeUndefined();
  });
});

describe("DocSurface", () => {
  test("renders the markdown body through the `.ov-md` doc article", () => {
    const { container } = render(<DocSurface props={{ content: "# Title\n\n- a\n- b" }} />);
    const article = container.querySelector(".ov-md");
    expect(article).not.toBeNull();
    expect(article?.querySelector(".md-h1")?.textContent).toBe("Title");
    expect(article?.querySelectorAll(".md-ul li")).toHaveLength(2);
  });

  test("shows the mono meta strip (filename · lines · reading) only when `file` is present", () => {
    const withFile = render(
      <DocSurface props={{ content: "# Notes\n\none\ntwo", file: "notes.md", lines: 12 }} />,
    );
    const meta = withFile.container.querySelector(".ov-md-meta");
    expect(meta).not.toBeNull();
    expect(meta?.querySelector(".ov-md-filename")?.textContent).toBe("notes.md");
    expect(meta?.textContent).toContain("12 lignes");
    expect(meta?.textContent).toContain("lecture ≈");
    // No `file` → no meta strip (just the body).
    const noFile = render(<DocSurface props={{ content: "# Notes" }} />);
    expect(noFile.container.querySelector(".ov-md-meta")).toBeNull();
    expect(noFile.container.querySelector(".ov-md")).not.toBeNull();
  });

  test("a non-string / missing content renders an empty doc rather than crashing", () => {
    const { container } = render(<DocSurface props={{ content: 42 }} />);
    expect(container.querySelector(".ov-md")).not.toBeNull();
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

  test("`bare` omits the wrapping `.ov-md` article (caller supplies its own)", () => {
    const { container } = render(<MarkdownSection props={{ content: "# Title" }} bare />);
    // No outer article — but the markdown heading still renders.
    expect(container.querySelector(".ov-md")).toBeNull();
    expect(container.querySelector(".md-h1")?.textContent).toBe("Title");
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
