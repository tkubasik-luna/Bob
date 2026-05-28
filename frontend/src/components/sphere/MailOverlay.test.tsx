import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { MailProps } from "../../types/ws";
import { MailOverlay } from "./MailOverlay";

/** Canonical fixture mirroring the design mockup (Marie Lefèvre, Q3
 * forecast, 2 attachments, PRIORITY flag). Matches the backend
 * `test_ui_registry::_mail_fixture` so the contract stays symmetrical
 * across the wire. */
const FIXTURE: MailProps = {
  from: {
    name: "Marie Lefèvre",
    email: "marie.lefevre@lunabee.com",
    role: "CFO · Lunabee",
  },
  receivedAt: "2026-05-28T14:22:00Z",
  subject: "Q3 forecast — final review before Thursday",
  bodyPreview:
    "Bob, can you have the deck ready by Thursday afternoon? I want to walk through it with Antoine before the board call.",
  flags: ["priority"],
  attachments: [
    { name: "Q3-forecast-v4.pdf", sizeBytes: 2_400_000, mime: "application/pdf" },
    { name: "Asia-deck-notes.md", sizeBytes: 18_432, mime: "text/markdown" },
  ],
  threadId: "thread-xyz-001",
  messageId: "msg-xyz-001",
  gmailWebUrl: "https://mail.google.com/mail/u/0/#inbox/thread-xyz-001",
};

describe("MailOverlay", () => {
  test("renders nothing when `mail === null`", () => {
    const onClose = vi.fn();
    const { container } = render(<MailOverlay mail={null} onClose={onClose} />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the full mockup chrome and body when mail is provided", () => {
    const onClose = vi.fn();
    const { container } = render(<MailOverlay mail={FIXTURE} onClose={onClose} />);
    expect(container.querySelector(".overlay-stage")).not.toBeNull();
    const card = container.querySelector(".overlay-card.surface-email");
    expect(card).not.toBeNull();
    // Corner brackets — same chrome as MarkdownOverlay.
    expect(card?.querySelectorAll(".ov-corner")).toHaveLength(4);
    // Header: source tag + INBOX chip + REF · MAI-xxxx + close button.
    expect(card?.querySelector(".ov-header .ov-source-tag")?.textContent).toBe("BOB · SURFACING");
    expect(card?.querySelector(".ov-header .ov-type-chip")?.textContent).toBe("INBOX");
    expect(card?.querySelector(".ov-header .ov-id-tag")?.textContent).toMatch(
      /^REF · MAI-[0-9A-F]{4}$/,
    );
    expect(card?.querySelector(".ov-header .ov-close")).not.toBeNull();
    // Body: avatar with initials, from name + role, address + timestamp,
    // priority flag, subject h2, snippet paragraph, 2 attachment chips.
    expect(card?.querySelector(".ov-email")).not.toBeNull();
    expect(card?.querySelector(".ov-avatar")?.textContent).toBe("ML");
    expect(card?.querySelector(".ov-email-name")?.textContent).toBe("Marie Lefèvre");
    expect(card?.querySelector(".ov-email-role")?.textContent).toBe("CFO · Lunabee");
    expect(card?.querySelector(".ov-email-addr")?.textContent).toContain(
      "marie.lefevre@lunabee.com",
    );
    const flags = card?.querySelectorAll(".ov-email-flags .ov-flag");
    expect(flags?.length).toBe(1);
    expect(flags?.[0].textContent).toBe("PRIORITY");
    expect(card?.querySelector("h2.ov-email-subject")?.textContent).toBe(
      "Q3 forecast — final review before Thursday",
    );
    expect(card?.querySelector(".ov-email-body")?.textContent).toContain(
      "Bob, can you have the deck ready",
    );
    const attachments = card?.querySelectorAll(".ov-attach");
    expect(attachments?.length).toBe(2);
    expect(attachments?.[0].querySelector(".ov-attach-name")?.textContent).toBe(
      "Q3-forecast-v4.pdf",
    );
    expect(attachments?.[1].querySelector(".ov-attach-name")?.textContent).toBe(
      "Asia-deck-notes.md",
    );
    // Footer: READ ALOUD / OPEN / DISMISS actions.
    expect(card?.querySelector(".ov-footer button[aria-label='read aloud']")).not.toBeNull();
    expect(card?.querySelector(".ov-footer button[aria-label='open']")).not.toBeNull();
    expect(card?.querySelector(".ov-footer button[aria-label='dismiss']")).not.toBeNull();
  });

  test("renders without flags or attachments when both are empty", () => {
    const onClose = vi.fn();
    const slim: MailProps = {
      ...FIXTURE,
      flags: [],
      attachments: [],
      // Drop `role` too — optional, must not crash and must not render the role span.
      from: { name: FIXTURE.from.name, email: FIXTURE.from.email },
    };
    const { container } = render(<MailOverlay mail={slim} onClose={onClose} />);
    // The flag list block is omitted entirely when empty (we don't want an
    // empty `.ov-email-flags` wrapper bumping the meta grid).
    expect(container.querySelector(".ov-email-flags")).toBeNull();
    // Same for attachments.
    expect(container.querySelector(".ov-email-attachments")).toBeNull();
    // Role span is omitted when missing.
    expect(container.querySelector(".ov-email-role")).toBeNull();
    // Subject + body still render.
    expect(container.querySelector(".ov-email-subject")?.textContent).toBe(FIXTURE.subject);
    expect(container.querySelector(".ov-email-body")?.textContent).toContain("Bob");
  });

  test("renders both `flags` and `attachments` as undefined (treated as empty)", () => {
    const onClose = vi.fn();
    // Strip the optional fields entirely — the backend defaults them to `[]`
    // but a defensive frontend handles `undefined` too.
    const sparse: MailProps = {
      from: { name: "Jane Doe", email: "jane@example.com" },
      receivedAt: "2026-05-28T09:00:00Z",
      subject: "Hi",
      bodyPreview: "Short note.",
      threadId: "t-1",
      messageId: "m-1",
      gmailWebUrl: "https://mail.google.com/mail/u/0/#inbox/t-1",
    };
    const { container } = render(<MailOverlay mail={sparse} onClose={onClose} />);
    expect(container.querySelector(".ov-email-flags")).toBeNull();
    expect(container.querySelector(".ov-email-attachments")).toBeNull();
    expect(container.querySelector(".ov-avatar")?.textContent).toBe("JD");
  });

  test("Escape keydown calls `onClose`", () => {
    const onClose = vi.fn();
    render(<MailOverlay mail={FIXTURE} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("does NOT register a global Escape listener when closed", () => {
    const onClose = vi.fn();
    render(<MailOverlay mail={null} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  test("clicking the `.overlay-stage` backdrop calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MailOverlay mail={FIXTURE} onClose={onClose} />);
    const stage = container.querySelector<HTMLDivElement>(".overlay-stage");
    expect(stage).not.toBeNull();
    if (stage) fireEvent.click(stage);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking inside the `.overlay-card` does NOT call `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MailOverlay mail={FIXTURE} onClose={onClose} />);
    const card = container.querySelector<HTMLDivElement>(".overlay-card");
    if (card) fireEvent.click(card);
    expect(onClose).not.toHaveBeenCalled();
  });

  test("clicking the header `×` button calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MailOverlay mail={FIXTURE} onClose={onClose} />);
    const close = container.querySelector<HTMLButtonElement>(".ov-close");
    if (close) fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking the footer `DISMISS` action calls `onClose`", () => {
    const onClose = vi.fn();
    const { container } = render(<MailOverlay mail={FIXTURE} onClose={onClose} />);
    const dismiss = container.querySelector<HTMLButtonElement>(
      '.ov-footer button[aria-label="dismiss"]',
    );
    if (dismiss) fireEvent.click(dismiss);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("clicking the footer `OPEN` action invokes `openExternal` with `gmailWebUrl`", () => {
    const onClose = vi.fn();
    const openExternal = vi.fn();
    const { container } = render(
      <MailOverlay mail={FIXTURE} onClose={onClose} openExternal={openExternal} />,
    );
    const open = container.querySelector<HTMLButtonElement>('.ov-footer button[aria-label="open"]');
    if (open) fireEvent.click(open);
    expect(openExternal).toHaveBeenCalledTimes(1);
    expect(openExternal).toHaveBeenCalledWith(FIXTURE.gmailWebUrl);
    // OPEN must NOT also dismiss — the user should be able to come back to the card.
    expect(onClose).not.toHaveBeenCalled();
  });

  test("`READ ALOUD` is rendered but is a no-op (placeholder for MVP)", () => {
    // Regression: the issue calls READ ALOUD out as a no-op. Make sure
    // clicking it doesn't dismiss the overlay or trigger an external open.
    const onClose = vi.fn();
    const openExternal = vi.fn();
    const { container } = render(
      <MailOverlay mail={FIXTURE} onClose={onClose} openExternal={openExternal} />,
    );
    const readAloud = container.querySelector<HTMLButtonElement>(
      '.ov-footer button[aria-label="read aloud"]',
    );
    expect(readAloud).not.toBeNull();
    if (readAloud) fireEvent.click(readAloud);
    expect(onClose).not.toHaveBeenCalled();
    expect(openExternal).not.toHaveBeenCalled();
  });

  test("REF marker is stable for a given messageId", () => {
    const onClose = vi.fn();
    const { container, rerender } = render(<MailOverlay mail={FIXTURE} onClose={onClose} />);
    const first = container.querySelector(".ov-id-tag")?.textContent;
    rerender(<MailOverlay mail={{ ...FIXTURE }} onClose={onClose} />);
    const second = container.querySelector(".ov-id-tag")?.textContent;
    expect(first).toBe(second);
  });
});
