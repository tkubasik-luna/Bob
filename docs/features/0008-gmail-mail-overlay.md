# Gmail Connector & Mail Overlay

Shipped on 2026-05-28 from PRD `prd/0007-gmail-mail-overlay.md`.

## What it does

The user can ask Bob in natural language — "trouve-moi le dernier mail
d'Holyana Callejon" — and Bob delegates the search to a background
sub-task that hits the user's Gmail account (read-only) and surfaces the
best match as a dedicated `Mail` overlay in the HUD. The overlay mirrors
the design mockup: corner brackets, avatar with sender initials, from
name + email + timestamp, priority/unread/starred flag pills, subject as
heading, snippet paragraph, attachment chips, and footer actions
(`READ ALOUD`, `OPEN ↗`, `DISMISS`). Bob speaks a short meta summary
("Mail de Holyana Callejon, sujet 'Q3 forecast', reçu jeudi à 14h22");
the body is never read aloud automatically. Clicking `OPEN` jumps to the
Gmail web thread in the default browser. On an empty result, Bob just
says "Aucun mail récent de {sender}" with no overlay. On auth or API
failures, Bob explains in French what to do — without ever showing a
broken state.

## Technical surface

- **New backend package — `bob.connectors.gmail`** — `auth` (OAuth2
  installed-app flow + runtime `get_credentials()` + CLI bootstrap via
  `python -m bob.connectors.gmail`), `client` (`GmailClient` wrapping
  `googleapiclient.discovery.build`, mockable at the HTTP transport
  layer via a `service_factory` test seam), `models` (`EmailMessage` /
  `Attachment` dataclasses + pure `from_gmail_payload` factory +
  `to_mail_props` adapter), `query_builder` (pure mapping of structured
  args to Gmail search syntax).
- **Error taxonomy** — `MissingCredentialsError`, `BootstrapRequiredError`,
  `RefreshFailedError`. Token file persisted with `chmod 0600`.
- **Sub-agent tool** — `gmail_search` (v1) registered in
  `build_default_subagent_registry()`. `GmailSearchArgs` Pydantic model
  with eight fields (`from_name`, `from_email`, `subject_contains`,
  `after`, `before`, `has_attachment`, `label`, `max_results` capped
  at 5). All-None rejection. Handler maps connector exceptions to
  structured error codes (`gmail_search_bootstrap_required`,
  `gmail_search_refresh_failed`, `gmail_search_auth_failed`,
  `gmail_search_api_unreachable`, `gmail_search_invalid_query`,
  `gmail_search_failed`).
- **Prompts** — `SUB_AGENT_V2_SYSTEM_PROMPT` (v3) carries the full
  email-lookup recipe: success path (progress thoughts "recherche
  Gmail" / "lecture du mail" → `done` with `ui_payload={"component":
  "Mail","props":...}`), empty-result branch, four error branches with
  exact French speech. Jarvis system prompt (`backend/prompts/
  system_chat.md`) gains a single capability line ("Bob peut retrouver
  des mails via une recherche en sous-tâche") — no tool name leaked.
- **UI component** — `MAIL` registered in `bob.ui_registry.build_registry()`
  with a strict JSON schema (`from {name,email,role?}`, `receivedAt` ISO
  8601, `subject`, `bodyPreview`, `flags[]` enum, `attachments`,
  `threadId`, `messageId`, `gmailWebUrl`). Validation rejects malformed
  props before reaching the frontend.
- **Frontend** — new `MailOverlay.tsx` mirrors `MarkdownOverlay`'s shell.
  `SphereUI` now dispatches both the streaming `ui_payload` frame and
  the final `assistant_msg.ui` on the `component` discriminator
  (`"Markdown"` → `MarkdownOverlay`, `"Mail"` → `MailOverlay`). Unknown
  components silently no-op. `ComponentDescriptor` typed as a
  discriminated union with a `MailProps` TS mirror.
- **Privacy** — debug events emitted by the runner pass any Mail
  `ui_payload` through `_redact_ui_payload_for_debug` before reaching
  the ring buffer / `/ws/debug` / file sink. `subject`, `bodyPreview`,
  `snippet`, and `body` are replaced with `<redacted-for-privacy>`;
  metadata (`messageId`, `threadId`, sender email, labels, attachment
  meta, `gmailWebUrl`, `receivedAt`) stays loggable. Chat-WS frames
  (`task_message`, `task_result`) are NOT redacted because the spoken
  meta summary must reach the user.
- **Dev trigger** — gated `?dev=1` Tweaks panel in `DevControls` has a
  "Surfaces (issue 0053)" row injecting a fixture Mail payload (Marie
  Lefèvre / Q3 forecast / 2 attachments / PRIORITY) so the overlay can
  be demoed without Gmail credentials.
- **Config** — `GMAIL_CREDENTIALS_PATH` (default
  `~/.bob/gmail/credentials.json`) and `GMAIL_TOKEN_PATH` (default
  `~/.bob/gmail/token.json`) added to `bob.config`, env-overridable.
- **Deps** — `google-auth`, `google-auth-oauthlib`,
  `google-api-python-client` added to `backend/pyproject.toml`.
- **README** — Gmail connector setup section: GCP project creation,
  Gmail API enable, OAuth consent screen, OAuth client (Desktop type),
  credentials placement, bootstrap script.

## Notable decisions

- **Read-only scope only** (`gmail.readonly`). Writing mail (send,
  reply, archive, label) is explicitly out of scope. Future iterations
  may add scoped tools but never share the same connector instance.
- **CLI bootstrap, not in-app OAuth.** The desktop app should never
  block itself behind an interactive browser flow. Refresh-token
  recovery (revoked token) tells the user to re-run the CLI script.
- **Sub-agent owns the tool, not Jarvis.** Routing pattern stays
  consistent: Jarvis decides via `spawn_task`, sub-agent executes.
  Jarvis's system prompt does not know the tool name.
- **Error codes prefixed `gmail_search_*`**, not the issue's
  speech-layer codes (`gmail_auth_expired` / `gmail_api_unreachable`).
  The prompt enumerates the full mapping from code → French speech, so
  the LLM bridges between them. Keeps the error taxonomy uniform with
  the rest of the sub-agent tool registry.
- **`from.role` omitted in MVP.** The Mail schema accepts it
  optionally; Gmail's API doesn't return it. Future enhancement via
  People API or a hand-curated contact directory could populate it.
- **Body shown as Gmail snippet (~150 chars).** Full HTML / plaintext
  body rendering deferred — would need iframe sandboxing.
- **`READ ALOUD` button is a visual no-op.** Wiring it to a Jarvis
  follow-up that speaks the full body is deferred to keep the demo
  scope tight.
- **`OPEN ↗` uses `window.open(url, '_blank', 'noopener,noreferrer')`,
  not Tauri shell-open.** The dynamic import of
  `@tauri-apps/plugin-shell` broke Vite's transform-time resolution and
  required a test-mocked seam; falling back to `window.open` was lower
  friction and survives the test boundary cleanly. Swap to Tauri
  shell-open later via the existing `openExternal` prop seam.
- **Privacy redaction lives at one chokepoint** — the runner's debug
  event emit site — rather than scattered across the connector, the
  handler, or the event bus. Single place to audit.
- **Multi-account assumed single (`accountIndex=0`).** The Gmail web URL
  is built as `https://mail.google.com/mail/u/0/#inbox/{threadId}`.
  Multi-account selection deferred.

## Issues

- `issues/0053-mail-ui-component-overlay.md` — Mail UI component &
  overlay — commit `5af1f39`
- `issues/0054-gmail-connector-package.md` — Gmail connector package
  (auth + client + models + query_builder + tests + README) —
  commit `1e5a699`
- `issues/0055-gmail-search-tool-e2e.md` — `gmail_search` tool wired to
  sub-agent runtime + prompts updated + reflection events —
  commit `8eef719`
- `issues/0056-gmail-error-empty-result-handling.md` — empty / auth /
  API / validation branches + debug-event privacy redaction —
  commit `0a822f2`

## HITL smoke tests (deferred to user)

- Create GCP project + OAuth client (Desktop type), download
  `credentials.json` to `~/.bob/gmail/`, run `python -m bob.connectors.gmail`,
  consent in browser, verify `~/.bob/gmail/token.json` exists with
  mode `0600`.
- Run a dev script: `from bob.connectors.gmail import auth, GmailClient;
  c = GmailClient(auth.get_credentials()); print([m.subject for m in
  c.search_messages('', max_results=3)])` — expect 3 inbox subjects.
- Query a known sender via Bob ("trouve-moi le dernier mail de
  {known sender}") — Mail overlay opens within ~2 s with correct data.
- Query a non-existent sender — Bob speaks "Aucun mail récent de…";
  no overlay opens.
- Revoke the OAuth refresh token in Google account settings, re-run
  the query — Bob speaks the bootstrap-required French speech, task
  marked `failed`.
