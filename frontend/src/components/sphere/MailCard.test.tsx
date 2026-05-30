import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { MailProps } from "../../types/ws";
import { MailCard } from "./MailCard";

/** Canonical fixture mirroring the design mockup (Marie Lefèvre, Q3 forecast,
 * 2 attachments, PRIORITY flag) — same shape the backend `to_mail_props`
 * emits. */
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

describe("MailCard", () => {
  test("renders the chrome-free mail body (no overlay chrome)", () => {
    const { container } = render(<MailCard props={FIXTURE} />);
    // The card body renders…
    expect(container.querySelector(".ov-email")).not.toBeNull();
    // …but NOT the overlay chrome (corner brackets / stage / header) — those
    // live once in SectionsOverlay, never per card.
    expect(container.querySelector(".overlay-stage")).toBeNull();
    expect(container.querySelector(".overlay-card")).toBeNull();
    expect(container.querySelector(".ov-corner")).toBeNull();
    expect(container.querySelector(".ov-header")).toBeNull();
  });

  test("renders avatar, sender, subject, snippet, flags, attachments", () => {
    const { container } = render(<MailCard props={FIXTURE} />);
    expect(container.querySelector(".ov-avatar")?.textContent).toBe("ML");
    expect(container.querySelector(".ov-email-name")?.textContent).toBe("Marie Lefèvre");
    expect(container.querySelector(".ov-email-role")?.textContent).toBe("CFO · Lunabee");
    expect(container.querySelector(".ov-email-addr")?.textContent).toContain(
      "marie.lefevre@lunabee.com",
    );
    const flags = container.querySelectorAll(".ov-email-flags .ov-flag");
    expect(flags.length).toBe(1);
    expect(flags[0].textContent).toBe("PRIORITY");
    expect(container.querySelector("h2.ov-email-subject")?.textContent).toBe(
      "Q3 forecast — final review before Thursday",
    );
    expect(container.querySelector(".ov-email-body")?.textContent).toContain(
      "Bob, can you have the deck ready",
    );
    const attachments = container.querySelectorAll(".ov-attach");
    expect(attachments.length).toBe(2);
    expect(attachments[0].querySelector(".ov-attach-name")?.textContent).toBe("Q3-forecast-v4.pdf");
    expect(attachments[1].querySelector(".ov-attach-name")?.textContent).toBe("Asia-deck-notes.md");
  });

  test("renders inline OPEN + READ ALOUD actions per card", () => {
    const { container } = render(<MailCard props={FIXTURE} />);
    expect(
      container.querySelector(".ov-email-actions button[aria-label='read aloud']"),
    ).not.toBeNull();
    expect(container.querySelector(".ov-email-actions button[aria-label='open']")).not.toBeNull();
  });

  test("clicking OPEN invokes `openExternal` with `gmailWebUrl` (test seam)", () => {
    const openExternal = vi.fn();
    const { container } = render(<MailCard props={FIXTURE} openExternal={openExternal} />);
    const open = container.querySelector<HTMLButtonElement>(
      ".ov-email-actions button[aria-label='open']",
    );
    if (open) fireEvent.click(open);
    expect(openExternal).toHaveBeenCalledTimes(1);
    expect(openExternal).toHaveBeenCalledWith(FIXTURE.gmailWebUrl);
  });

  test("clicking READ ALOUD invokes the `onReadAloud` seam with the mail", () => {
    const onReadAloud = vi.fn();
    const { container } = render(<MailCard props={FIXTURE} onReadAloud={onReadAloud} />);
    const readAloud = container.querySelector<HTMLButtonElement>(
      ".ov-email-actions button[aria-label='read aloud']",
    );
    if (readAloud) fireEvent.click(readAloud);
    expect(onReadAloud).toHaveBeenCalledTimes(1);
    expect(onReadAloud).toHaveBeenCalledWith(FIXTURE);
  });

  test("READ ALOUD is a no-op when no seam is provided (does not throw)", () => {
    const { container } = render(<MailCard props={FIXTURE} />);
    const readAloud = container.querySelector<HTMLButtonElement>(
      ".ov-email-actions button[aria-label='read aloud']",
    );
    expect(() => {
      if (readAloud) fireEvent.click(readAloud);
    }).not.toThrow();
  });

  test("omits flag list / attachment list / role when empty or missing", () => {
    const slim: MailProps = {
      ...FIXTURE,
      flags: [],
      attachments: [],
      from: { name: FIXTURE.from.name, email: FIXTURE.from.email },
    };
    const { container } = render(<MailCard props={slim} />);
    expect(container.querySelector(".ov-email-flags")).toBeNull();
    expect(container.querySelector(".ov-email-attachments")).toBeNull();
    expect(container.querySelector(".ov-email-role")).toBeNull();
    // Subject + body still render.
    expect(container.querySelector(".ov-email-subject")?.textContent).toBe(FIXTURE.subject);
  });

  test("treats `flags`/`attachments` undefined as empty", () => {
    const sparse: MailProps = {
      from: { name: "Jane Doe", email: "jane@example.com" },
      receivedAt: "2026-05-28T09:00:00Z",
      subject: "Hi",
      bodyPreview: "Short note.",
      threadId: "t-1",
      messageId: "m-1",
      gmailWebUrl: "https://mail.google.com/mail/u/0/#inbox/t-1",
    };
    const { container } = render(<MailCard props={sparse} />);
    expect(container.querySelector(".ov-email-flags")).toBeNull();
    expect(container.querySelector(".ov-email-attachments")).toBeNull();
    expect(container.querySelector(".ov-avatar")?.textContent).toBe("JD");
  });

  test("renders nothing for a malformed props bag (defensive guard)", () => {
    // A payload missing the required `from`/`gmailWebUrl` must not crash the
    // sections stack — it collapses to null.
    const { container } = render(<MailCard props={{ subject: "no from" }} />);
    expect(container.firstChild).toBeNull();
  });
});
