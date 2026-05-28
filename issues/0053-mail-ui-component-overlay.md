## Parent

`prd/0007-gmail-mail-overlay.md`

## What to build

Ship the `Mail` UI surface end-to-end without any Gmail involvement: a new component descriptor on the backend registry and a new overlay component on the frontend, wired through the existing `say.ui` channel.

Backend: register a `Mail` `UIComponent` in `bob.ui_registry.build_registry()` with a strict JSON schema for props (`from {name,email,role?}`, `receivedAt` ISO 8601, `subject`, `bodyPreview`, `flags[]` enum of `priority|unread|starred`, `attachments[{name,sizeBytes,mime}]`, `threadId`, `messageId`, `gmailWebUrl`). Schema is the canonical source for both runtime validation via `coerce_component_descriptor` and frontend typing.

Frontend: build `MailOverlay.tsx` under `frontend/src/components/sphere/`, mirroring `MarkdownOverlay`'s structure (corner brackets, header strip with `BOB · SURFACING / INBOX / REF · MAI-xxxx` + close, body, footer with `READ ALOUD ↵`, `OPEN ↗`, `DISMISS ESC` actions). Body matches `Design Mockup/overlay.jsx` `EmailBody`: avatar with sender initials, from name + role-if-present, address + relative timestamp, flag pills, subject as `h2`, snippet paragraph, attachment chips. `OPEN` opens `gmailWebUrl` via Tauri shell-open (or `window.open` fallback). `READ ALOUD` is a no-op placeholder for MVP. `DISMISS` / Escape / backdrop click closes.

Extend `SphereUI` to dispatch overlay rendering on `ComponentDescriptor.component` (`"Markdown"` → `MarkdownOverlay`, `"Mail"` → `MailOverlay`). Both the streaming `ui_payload` frame and the `assistant_msg.ui` fallback share the dispatch.

Extend `frontend/src/types/ws.ts` `ComponentDescriptor` to a discriminated union typing both surfaces, with a TypeScript `MailProps` mirror of the backend JSON schema.

A dev-only trigger (debug button, devtools snippet, or test sub-agent stub) emits `{component:"Mail", props: fixture}` so the overlay can be demoed without Gmail credentials.

## Acceptance criteria

- [ ] `Mail` component registered in `bob.ui_registry.build_registry()` with full JSON schema for props (required: from, receivedAt, subject, bodyPreview, threadId, messageId, gmailWebUrl; optional: flags, attachments, from.role).
- [ ] Backend schema validation rejects malformed Mail props (missing required field, bad flags enum, malformed date).
- [ ] `MailOverlay` component renders all mockup elements: corner brackets, header with chip + REF tag + close, avatar + from + role + addr + timestamp, flag pills, subject h2, snippet paragraph, attachment chips, footer actions.
- [ ] `SphereUI` dispatches `component="Markdown"` to `MarkdownOverlay` and `component="Mail"` to `MailOverlay`; both `ui_payload` and `assistant_msg.ui` trigger paths covered.
- [ ] `MailProps` TypeScript type in `frontend/src/types/ws.ts` mirrors the backend schema.
- [ ] `OPEN` button invokes browser open on `gmailWebUrl`; `DISMISS` / Esc / backdrop click closes overlay; `READ ALOUD` is rendered but no-op (placeholder).
- [ ] Dev trigger emits a fixture Mail payload and the overlay opens with mockup-style data (Marie Lefèvre, Q3 forecast, 2 attachments, PRIORITY flag).
- [ ] Vitest tests: `MailOverlay` renders with full props, with empty attachments/flags, closes on Esc, closes on DISMISS, closes on backdrop click, OPEN calls browser-open with `gmailWebUrl`.
- [ ] Backend unit test: `Mail` schema validation passes on canonical fixture, rejects each mandatory-field-missing case and each enum/format violation.
- [ ] Frontend test: `SphereUI` overlay dispatcher routes Markdown vs Mail correctly.

## Blocked by

None - can start immediately.
