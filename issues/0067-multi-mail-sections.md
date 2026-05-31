## Parent

`prd/0010-adaptive-composite-ui.md`

## What to build

Surface every matched mail as its own section, fixing the core bug ("3 derniers mails" showing only one). The deterministic Gmail projector emits one `Mail` section per returned message, and the frontend renders them as a stack of self-contained mail cards inside the unified `SectionsOverlay`.

End-to-end path:

- **Backend** — `project_gmail_search` produces `[Mail(props) for props in messages]` (in result order) instead of `messages[0]` only. The transcript digest and the deterministic spoken summary ("N email(s) trouvé(s)…") are unchanged. A single `result_ref` in the sub-agent `done` action expands to the full projected section list (the LLM never enumerates messages or writes props).
- **Frontend** — the mail body is extracted into a `MailCard` (avatar, sender, subject, snippet, flags, attachments) carrying its **inline actions** (OPEN to `gmailWebUrl`, READ ALOUD), without overlay chrome. `Mail` is registered in the section registry as a **structured** component, so a list containing a mail auto-opens the overlay. The old standalone `MailOverlay` is removed.

Verifiable on its own: "donne-moi mes 3 derniers mails" → 3 stacked mail cards, each with its own OPEN / READ ALOUD; a single-mail request renders a one-section view equivalent to the old overlay.

## Acceptance criteria

- [ ] `project_gmail_search` emits one `Mail` section per returned message, preserving order; a zero-result search yields no deliverable (`None` / empty).
- [ ] The transcript digest and deterministic spoken summary are unchanged.
- [ ] A `done` action with a single `result_ref` expands to the full projected section list for that result.
- [ ] `MailCard` renders the mail body with inline OPEN (routes to `gmailWebUrl` via the test seam) and READ ALOUD actions, per card, without overlay chrome.
- [ ] `Mail` is registered as a `structured` section; a list containing a `Mail` auto-opens the overlay regardless of text heuristic.
- [ ] "donne-moi mes 3 derniers mails" renders 3 stacked `Mail` cards in `SectionsOverlay`.
- [ ] A single-mail result renders a one-section view with no regression versus the prior overlay.
- [ ] `MailOverlay` (standalone mono-component overlay) is removed.
- [ ] Tests cover: projector multi-Mail (N messages → N sections, ordering, props validity, empty case), MailCard inline actions + flags/attachments, structured auto-open dispatch.

## Blocked by

- `issues/0066-sections-list-pipeline-markdown.md`
