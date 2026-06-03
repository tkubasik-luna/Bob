import { describe, expect, it } from "vitest";
import type { ComponentDescriptor, MailProps } from "../types/ws";
import { type DeliverableCardTask, toCard } from "./deliverableCard";

/** Build a Mail descriptor with sane defaults; override what a case cares
 * about (subject is what the content-summary fallback reads). */
function mail(overrides: Partial<MailProps> = {}): ComponentDescriptor {
  const props: MailProps = {
    from: { name: "Daniela Marsh", email: "daniela.marsh@lunabee.com", role: "Finance" },
    receivedAt: "2026-05-28T14:22:00Z",
    subject: "Budget v4 — à valider avant jeudi",
    bodyPreview: "Peux-tu faire valider la v4…",
    threadId: "t1",
    messageId: "m1",
    gmailWebUrl: "https://mail.google.com/x",
    ...overrides,
  };
  return { component: "Mail", props };
}

/** Build a Markdown descriptor. */
function markdown(content: string): ComponentDescriptor {
  return { component: "Markdown", props: { content } };
}

const task = (title: string, goal?: string): DeliverableCardTask => ({ title, goal });

describe("deliverableCard.toCard", () => {
  it("single mail → mail type, title = task title, goal-driven sub, sections passed through", () => {
    const sections = [mail({ subject: "Revue Q3 — déplacée à jeudi 15h" })];
    const card = toCard(sections, task("Trier les mails", "Trie ce que Daniela a envoyé"));

    expect(card.title).toBe("Trier les mails");
    expect(card.sub).toBe("Trie ce que Daniela a envoyé");
    expect(card.type).toBe("mail");
    // The overlay consumes the same descriptors unchanged.
    expect(card.sections).toBe(sections);
    expect(card.sections).toHaveLength(1);
  });

  it("multi-mail (homogeneous) → still mail type (dominant), one card for the whole stack", () => {
    const sections = [
      mail({ subject: "Budget v4 — à valider" }),
      mail({ subject: "Revue jeudi" }),
      mail({ subject: "Contrat renouvelé" }),
    ];
    const card = toCard(sections, task("Mails de la semaine", "Résumé des 3 derniers mails"));

    expect(card.type).toBe("mail");
    expect(card.title).toBe("Mails de la semaine");
    expect(card.sub).toBe("Résumé des 3 derniers mails");
    expect(card.sections).toHaveLength(3);
  });

  it("heterogeneous composite (mail + markdown) → composite glyph", () => {
    const sections = [mail({ subject: "Daniela — budget" }), markdown("## Synthèse\n- point un")];
    const card = toCard(sections, task("Récap board"));

    // Mixed section types yield the composite glyph regardless of order/count.
    expect(card.type).toBe("composite");
    expect(card.title).toBe("Récap board");
    expect(card.sections).toHaveLength(2);
  });

  it("composite is order-independent (markdown first, then mail) → still composite", () => {
    const sections = [markdown("Notes"), mail()];
    const card = toCard(sections, task("Mix"));
    expect(card.type).toBe("composite");
  });

  it("Bob ui_payload (single Markdown wrapped as list-of-one) → doc type, content-summary sub", () => {
    // Bob streams a single `ui_payload` descriptor; the ingest layer wraps it
    // into a list-of-one. No real Task → a synthetic task carries the title and
    // no goal, so the sub falls back to the first non-empty markdown line.
    const sections = [markdown("Daniela — 3 messages cette semaine\n\nDétails plus bas.")];
    const card = toCard(sections, task("Réponse de Bob"));

    expect(card.type).toBe("doc");
    expect(card.title).toBe("Réponse de Bob");
    expect(card.sub).toBe("Daniela — 3 messages cette semaine");
    expect(card.sections).toHaveLength(1);
  });

  it("sub falls back to the Mail subject when the task has no goal", () => {
    const card = toCard([mail({ subject: "Revue Q3 — jeudi 15h" })], task("Mail trié"));
    expect(card.sub).toBe("Revue Q3 — jeudi 15h");
  });

  it("sub falls back to a count label when neither goal nor a usable summary exists", () => {
    // An unknown/forward-compat component has no readable summary fields.
    const unknown: ComponentDescriptor = { component: "FutureWidget", props: {} };
    const card = toCard([unknown, unknown], task("Artefact futur"));
    expect(card.sub).toBe("2 éléments");
    // A single unknown section → singular label.
    const single = toCard([unknown], task("Artefact futur"));
    expect(single.sub).toBe("1 élément");
  });

  it("unknown component maps to the generic doc type (no crash, forward-compat)", () => {
    const unknown: ComponentDescriptor = { component: "FutureWidget", props: { foo: 1 } };
    const card = toCard([unknown], task("X"));
    expect(card.type).toBe("doc");
  });

  it("empty deliverable → doc type + count sub (defensive, never throws)", () => {
    const card = toCard([], task("Vide"));
    expect(card.type).toBe("doc");
    expect(card.sub).toBe("0 éléments");
    expect(card.sections).toHaveLength(0);
  });

  it("prefers goal over the content summary when both are present", () => {
    const card = toCard([mail({ subject: "SUBJECT" })], task("T", "GOAL WINS"));
    expect(card.sub).toBe("GOAL WINS");
  });
});
