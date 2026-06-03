import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { ComponentDescriptor, MailProps } from "../../types/ws";
import { SectionsOverlay } from "./SectionsOverlay";

const MARKDOWN_SECTION: ComponentDescriptor = {
  component: "Markdown",
  props: { content: "# Big heading\n\n- one\n- two" },
};

const MAIL_FIXTURE: MailProps = {
  from: { name: "Marie Lefèvre", email: "marie@lunabee.com" },
  receivedAt: "2026-05-28T14:22:00Z",
  subject: "Q3 forecast",
  bodyPreview: "Deck for Thursday.",
  threadId: "t-1",
  messageId: "m-1",
  gmailWebUrl: "https://mail.google.com/mail/u/0/#inbox/t-1",
};
const MAIL_SECTION: ComponentDescriptor = { component: "Mail", props: MAIL_FIXTURE };

describe("SectionsOverlay", () => {
  test("renders nothing when `sections === null`", () => {
    const { container } = render(<SectionsOverlay sections={null} onClose={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  test("renders nothing for an empty list", () => {
    const { container } = render(<SectionsOverlay sections={[]} onClose={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the mockup chrome (scrim + beam, corner brackets, mono header, footer)", () => {
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={vi.fn()} />,
    );
    // The stage carries the `p3d-ov` scoping class (so the ported `ov-*` rules
    // win over the legacy ones in hud.css) plus the mockup `ov-stage`.
    const stage = container.querySelector(".p3d-ov.ov-stage");
    expect(stage).not.toBeNull();
    expect(container.querySelector(".ov-scrim")).not.toBeNull();
    expect(container.querySelector(".ov-beam")).not.toBeNull();
    const card = container.querySelector(".ov-card");
    expect(card).not.toBeNull();
    // Four corner brackets — TL/TR/BL/BR.
    expect(card?.querySelectorAll(".ov-corner")).toHaveLength(4);
    // Mono header: `BOB · GÉNÉRÉ` source tag + a type chip + `RÉF · SEC-XXXX`.
    expect(card?.querySelector(".ov-header .ov-source-tag")?.textContent).toBe("BOB · GÉNÉRÉ");
    expect(card?.querySelector(".ov-header .ov-type-chip")?.textContent).toBe("FICHIER");
    expect(card?.querySelector(".ov-header .ov-id-tag")?.textContent).toMatch(
      /^RÉF · SEC-[0-9A-F]{4}$/,
    );
    expect(card?.querySelector(".ov-footer")).not.toBeNull();
  });

  test("header chip reflects the stack type (Mail → BOÎTE, mixed → SURFACE)", () => {
    const mail = render(<SectionsOverlay sections={[MAIL_SECTION]} onClose={vi.fn()} />);
    expect(mail.container.querySelector(".ov-type-chip")?.textContent).toBe("BOÎTE");
    const mixed = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION, MAIL_SECTION]} onClose={vi.fn()} />,
    );
    expect(mixed.container.querySelector(".ov-type-chip")?.textContent).toBe("SURFACE");
  });

  test("footer exposes LIRE À VOIX HAUTE / OUVRIR / FERMER actions", () => {
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={vi.fn()} />,
    );
    const labels = Array.from(
      container.querySelectorAll(".ov-footer .ov-action span:last-child"),
    ).map((s) => s.textContent);
    expect(labels).toEqual(["LIRE À VOIX HAUTE", "OUVRIR", "FERMER"]);
  });

  test("renders a Markdown section through the registry (Document surface)", () => {
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={vi.fn()} />,
    );
    const article = container.querySelector(".ov-section .ov-md");
    expect(article).not.toBeNull();
    expect(article?.querySelector(".md-h1")?.textContent).toBe("Big heading");
    expect(article?.querySelectorAll(".md-ul li")).toHaveLength(2);
  });

  test("renders multiple sections in list order inside one shell (stack preserved)", () => {
    const sections: ComponentDescriptor[] = [
      { component: "Markdown", props: { content: "# First" } },
      { component: "Markdown", props: { content: "# Second" } },
    ];
    const { container } = render(<SectionsOverlay sections={sections} onClose={vi.fn()} />);
    // Exactly one shell …
    expect(container.querySelectorAll(".ov-card")).toHaveLength(1);
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
    // The scroll affordance lives on `.ov-body` (overflow-y: auto in the
    // co-located CSS); we assert the structural class is present.
    expect(container.querySelector(".ov-card .ov-body.ov-sections")).not.toBeNull();
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

  test("clicking the `.ov-stage` backdrop calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />,
    );
    const stage = container.querySelector<HTMLDivElement>(".ov-stage");
    if (stage) fireEvent.click(stage);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking inside the `.ov-card` does NOT call `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />,
    );
    const card = container.querySelector<HTMLDivElement>(".ov-card");
    if (card) fireEvent.click(card);
    expect(onClose).not.toHaveBeenCalled();
  });

  test("clicking the header `✕` button calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={onClose} />,
    );
    const close = container.querySelector<HTMLButtonElement>(".ov-close");
    if (close) fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking the footer `FERMER` action calls `onClose`", () => {
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

  test("LIRE À VOIX HAUTE feeds the flattened artifact text to the `speak` seam", () => {
    const speak = vi.fn();
    const { container } = render(
      <SectionsOverlay sections={[MARKDOWN_SECTION]} onClose={vi.fn()} speak={speak} />,
    );
    const readAloud = container.querySelector<HTMLButtonElement>(
      '.ov-footer button[aria-label="read aloud"]',
    );
    if (readAloud) fireEvent.click(readAloud);
    expect(speak).toHaveBeenCalledTimes(1);
    // Markdown syntax is stripped before speaking (no leading `#` / `-`).
    const spoken = speak.mock.calls[0][0] as string;
    expect(spoken).toContain("Big heading");
    expect(spoken).toContain("one");
    expect(spoken).not.toContain("#");
    expect(spoken).not.toMatch(/^- /m);
  });

  test("OUVRIR browses to the first openable artifact (a Mail's gmailWebUrl)", () => {
    const openExternal = vi.fn();
    const { container } = render(
      <SectionsOverlay
        sections={[MARKDOWN_SECTION, MAIL_SECTION]}
        onClose={vi.fn()}
        openExternal={openExternal}
      />,
    );
    const open = container.querySelector<HTMLButtonElement>('.ov-footer button[aria-label="open"]');
    if (open) fireEvent.click(open);
    expect(openExternal).toHaveBeenCalledTimes(1);
    expect(openExternal).toHaveBeenCalledWith(MAIL_FIXTURE.gmailWebUrl);
  });

  test("OUVRIR is inert when the stack has nothing openable (no throw, no call)", () => {
    const openExternal = vi.fn();
    const { container } = render(
      <SectionsOverlay
        sections={[MARKDOWN_SECTION]}
        onClose={vi.fn()}
        openExternal={openExternal}
      />,
    );
    const open = container.querySelector<HTMLButtonElement>('.ov-footer button[aria-label="open"]');
    expect(() => {
      if (open) fireEvent.click(open);
    }).not.toThrow();
    expect(openExternal).not.toHaveBeenCalled();
  });
});
