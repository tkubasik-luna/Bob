# 0007 — Gmail Connector & Mail Overlay

## Problem Statement

Today Bob can answer questions and spawn research sub-tasks, but cannot reach into the user's actual data. The user has years of email in Gmail (Lunabee Workspace account) and frequently needs to surface a specific message — "trouve-moi le dernier mail d'Holyana Callejon" — without breaking flow to open the Gmail web UI, type a search query, and scroll. From the user's perspective:

- The conversational assistant feels disconnected from real life as long as it has no access to mail, which is one of the highest-volume sources of context (subjects, attachments, threads with collaborators).
- Showing a found email as plain markdown in the existing Markdown overlay loses fidelity: the "from" identity, priority flag, attachments, and meta (received time, thread URL) get flattened into prose and become harder to scan in one glance.
- A natural-language mail search ("le dernier mail de X" / "the latest from Y about Z") that takes 1–2 s should not block the conversation thread; the user expects to keep talking while Bob fetches the message in the background.
- The HUD already has a polished Mail overlay design in the mockup (`Design Mockup/overlay.jsx`, `EmailBody`) that is not yet wired to anything: corner-bracketed card, avatar, from/role, addr + timestamp, PRIORITY flag, subject as h2, body preview, attachments chips, action footer (READ ALOUD / OPEN / DISMISS). Reusing it gives the user immediate value with no new design work.

## Solution

Add a Gmail read-only connector and a Mail overlay surface to Bob:

- The user says or types something like "trouve-moi le dernier mail d'Holyana Callejon". Jarvis recognises the intent, replies with a short acknowledgement ("Je cherche…"), and delegates the work to a background sub-task. The sphere transitions to a busy state and a task row appears in the HudTasks panel.
- The sub-task calls Gmail's API (read-only scope), picks the best matching message, and emits its result as a `Mail` UI component via the existing `say.ui` channel.
- The HUD opens the Mail overlay automatically when the sub-task finishes, populated with from name/email/role-if-known, received timestamp, subject, snippet body, attachments meta, flags (priority/unread/starred), threadId, and a pre-built Gmail web URL.
- Bob speaks a short meta summary ("Mail d'Holyana Callejon, sujet 'Q3 forecast', reçu jeudi à 14h22"). The body is not read aloud by default — the overlay's READ ALOUD button is the affordance for that.
- Clicking OPEN ↗ opens the Gmail web thread in the user's default browser. Clicking DISMISS / pressing Escape closes the overlay.
- If no matching mail is found, no overlay opens — Bob simply says "Aucun mail récent de Holyana Callejon."
- First-time setup is a one-shot CLI step: the user creates a Google Cloud OAuth client (Desktop type) in their own GCP project, downloads `credentials.json` to `~/.bob/gmail/`, then runs `python -m bob.connectors.gmail.auth`. A browser opens, the user consents to `gmail.readonly`, and a refresh token is persisted to `~/.bob/gmail/token.json` (chmod 0600). Subsequent app runs reuse the cached token (google-auth handles silent refresh).

## User Stories

1. As a Bob user, I want to say "trouve-moi le dernier mail d'Holyana Callejon" and have the Mail overlay open with that mail, so that I do not have to leave the HUD to consult Gmail.
2. As a Bob user, I want the search to happen as a background task with a visible task row, so that I can keep conversing with Bob while it works.
3. As a Bob user, I want Bob to speak a short meta summary of the mail when found (from / subject / when), so that I get the gist without reading.
4. As a Bob user, I want the Mail overlay to match the HUD mockup (corner brackets, avatar, from + role, addr + timestamp, PRIORITY flag, subject, body preview, attachments, footer actions), so that mail surfaces feel native to Bob, not bolted-on.
5. As a Bob user, I want the body shown in the overlay to be the Gmail-native snippet (≈150 chars), so that the card stays compact and scannable on first glance.
6. As a Bob user, I want a READ ALOUD button in the overlay footer, so that I can ask Bob to speak the full body on demand instead of by default.
7. As a Bob user, I want an OPEN button that takes me to the Gmail web thread, so that I can reply or archive in one click when needed.
8. As a Bob user, I want a DISMISS / Escape action that closes the overlay cleanly, so that I can return to the sphere without leftover UI.
9. As a Bob user, I want Bob to say "Aucun mail récent de Holyana Callejon" with no overlay opened when nothing matches, so that the HUD stays uncluttered on null results.
10. As a Bob user, I want my OAuth consent to happen once via a CLI bootstrap script, so that the desktop app never blocks itself behind an interactive browser flow.
11. As a Bob user, I want my refresh token to live in `~/.bob/gmail/token.json` with restrictive permissions, so that a casual file leak does not expose my inbox.
12. As a Bob user, I want the connector to use only `gmail.readonly` scope, so that the worst-case blast radius if anything goes wrong is "read", not "delete" or "send".
13. As a Bob user, I want the Mail overlay's "from" line to show name + email + (when available) role, so that I can identify the sender at a glance.
14. As a Bob user, I want attachments shown as chips with filename + size, so that I can spot whether the mail has an attached PDF / md / image without opening it.
15. As a Bob user, I want a PRIORITY flag rendered when Gmail marks the mail important (label `IMPORTANT` or starred), so that I see signal hierarchy at a glance.
16. As a Bob user, I want Bob to surface a live status line ("recherche Gmail…", "lecture du mail…") in the task row while the background task runs, so that I know what stage of work is in flight.
17. As a Bob user, I want my long-running task on Gmail to never lock up Jarvis — I want to keep asking unrelated things while the search runs, so that the assistant feels responsive.
18. As a Bob user, I want a graceful spoken error ("Je n'ai pas pu joindre Gmail à l'instant") when the API call fails (network down, token revoked), so that I am never left waiting silently on a doomed task.
19. As a Bob user, I want to know how to re-run the OAuth bootstrap script if my refresh token gets revoked, so that I can recover without grepping internals.
20. As a Bob user, I want the connector to only ever READ mail in this iteration — no archive, no label change, no send — so that I can trust Bob with my inbox before any write-scope feature lands.
21. As a developer, I want the Gmail connector encapsulated in a `bob.connectors.gmail` package with `auth`, `client`, and `models` modules, so that adding future connectors (Calendar, Drive, Slack) follows a consistent shape.
22. As a developer, I want a `GmailClient` interface deep enough to mock cleanly (single method `search_messages(query, max_results) -> list[EmailMessage]` and `get_message(id) -> EmailMessage`), so that tests can run without a live Gmail account.
23. As a developer, I want an `EmailMessage` domain model that is decoupled from the Gmail API JSON shape, so that frontend props derive from the domain model and not directly from Google's wire format.
24. As a developer, I want a `to_mail_props(EmailMessage) -> MailProps` adapter, so that the frontend contract is owned by Bob, not by Google.
25. As a developer, I want `gmail_search` registered as a sub-agent-side tool only (not Jarvis-side), so that the routing pattern stays consistent: Jarvis decides, sub-agent executes.
26. As a developer, I want the `gmail_search` tool args structured (Pydantic model with `from_name?`, `from_email?`, `subject_contains?`, `after?`, `before?`, `has_attachment?`, `label?`, `max_results=1`), so that the LLM cannot hallucinate raw Gmail search syntax.
27. As a developer, I want the tool args mapped to Gmail's search syntax inside the tool handler, so that escape/quoting bugs live in one tested place.
28. As a developer, I want a new `Mail` component registered in `ui_registry.build_registry()` with a strict JSON schema (from, receivedAt, subject, bodyPreview, flags, attachments, threadId, messageId, gmailWebUrl), so that bad LLM output fails validation before reaching the frontend.
29. As a developer, I want the backend to pre-compute the Gmail web URL (from threadId + accountIndex), so that the frontend stays ignorant of Gmail URL patterns.
30. As a developer, I want a new `MailOverlay` React component that mirrors `MarkdownOverlay`'s structure and styling pattern (corner brackets, header, body, footer, Esc/backdrop close), so that overlays remain visually consistent.
31. As a developer, I want `SphereUI` to dispatch overlay rendering on the `component` discriminator (`"Markdown"` → `MarkdownOverlay`, `"Mail"` → `MailOverlay`), so that adding a third surface later is a pattern, not a refactor.
32. As a developer, I want the `ui_payload` WS frame and `assistant_msg.ui` fallback to flow through the existing path (no new frame type), so that the Mail surface inherits streaming + replay + dedupe for free.
33. As a developer, I want Jarvis's system prompt to mention "you can find emails for the user via spawn_task" without specifying tool names, so that prompt churn stays low.
34. As a developer, I want the sub-agent's system prompt to mention `gmail_search` + the `Mail` component contract (use Mail UI for any email result), so that the LLM picks the right surface.
35. As a developer, I want OAuth credential paths configurable via `GMAIL_CREDENTIALS_PATH` and `GMAIL_TOKEN_PATH` env vars (defaults `~/.bob/gmail/credentials.json` and `~/.bob/gmail/token.json`), so that tests and dev setups can override.
36. As a developer, I want the OAuth bootstrap to live as `python -m bob.connectors.gmail.auth`, so that the same module owns both runtime refresh and one-shot consent.
37. As a developer, I want `auth.get_credentials()` to handle: missing token (raise with actionable message → run bootstrap), token present + valid (return), token present + expired + refreshable (refresh and persist), refresh token invalid (raise with actionable message → re-run bootstrap), so that the runtime path has no silent failure modes.
38. As a developer, I want a graceful `gmail_search` failure to surface as a structured sub-agent tool error → sub-agent emits a `say` with apologetic speech and no `ui` → Jarvis user-facing speech remains coherent, so that no broken state ever reaches the user.
39. As a developer, I want each module (`auth`, `client`, `models`, tool handler, `MailOverlay` component) covered by unit tests, so that future refactors of any one piece do not silently break the chain.
40. As a developer, I want tests to mock the Gmail HTTP layer (not patch `googleapiclient` internals), so that tests survive lib upgrades.

## Implementation Decisions

### Architecture overview

The feature spans three concentric layers:

1. **Gmail connector** (new package `bob.connectors.gmail`) — authoritative interface between Bob and Gmail. Owns OAuth, query construction, domain model.
2. **Sub-agent tool surface** — a new `gmail_search` tool registered in the sub-agent tool registry. Bridges the connector to LLM tool-calling.
3. **UI surface** — a new `Mail` UI component descriptor in the backend registry + a new `MailOverlay` React component in the Sphere HUD, dispatched from the existing `say.ui` channel.

Jarvis routing is unchanged: a user asking for an email match triggers `spawn_task`. The sub-agent receives the goal, calls `gmail_search`, then concludes with `say(speech="…", ui={component:"Mail", props:{…}})`.

### Modules

**New backend package — `bob.connectors.gmail`:**

- **`auth`** — Single responsibility: produce a refreshed `google.oauth2.credentials.Credentials` object on demand. Functions: `get_credentials() -> Credentials` (runtime path — load token, refresh if needed, persist if refreshed, raise actionable error if bootstrap required); `run_bootstrap() -> None` (one-shot CLI entry: load `credentials.json`, run installed-app flow on localhost, persist `token.json` with chmod 0600). Module is also a `python -m` entry (`__main__`) for the bootstrap script. Encapsulates token file paths (env-configurable), refresh logic, error taxonomy (`MissingCredentialsError`, `BootstrapRequiredError`, `RefreshFailedError`).
- **`client`** — Single responsibility: query Gmail. Class `GmailClient(credentials: Credentials)`. Public methods: `search_messages(query: str, max_results: int = 1) -> list[EmailMessage]`, `get_message(message_id: str) -> EmailMessage`. Internally uses `googleapiclient.discovery.build("gmail","v1",credentials=…)`. Translates Gmail's `users().messages().list()` + `get()` JSON into `EmailMessage` instances via `models.from_gmail_payload()`. Deep module — single interface, mockable at the HTTP transport layer.
- **`models`** — `EmailMessage` dataclass with fields: `id`, `thread_id`, `from_name`, `from_email`, `received_at` (datetime), `subject`, `snippet`, `labels: list[str]`, `attachments: list[Attachment]`. `Attachment` dataclass: `filename`, `size_bytes`, `mime_type`, `attachment_id`. Factory `from_gmail_payload(payload: dict) -> EmailMessage` (pure function, easy unit tests on canned fixtures). Adapter `to_mail_props(msg: EmailMessage, account_index: int = 0) -> dict` produces the dict that becomes the `Mail` UI component `props`; includes flags derivation (`labels` → `priority|unread|starred`) and pre-built `gmailWebUrl`.
- **`query_builder`** (optional helper) — Pure function `build_query(from_name=None, from_email=None, subject_contains=None, after=None, before=None, has_attachment=None, label=None) -> str` producing a Gmail search string. Centralises escaping/quoting. Heavily unit-tested. May start inline in the tool handler and be extracted later if it grows.

**New backend wiring:**

- **`bob.sub_agent.tool_registry`** — Add `build_gmail_search_tool()` builder returning a `SubAgentToolDefinition(name="gmail_search", version="v1", args_model=GmailSearchArgs, handler=_gmail_search_handler, description=…)`. Register it inside `build_default_subagent_registry()`. Handler: validate args, build query, instantiate `GmailClient(auth.get_credentials())`, run `search_messages(query, max_results=args.max_results)`, return structured result. On 0 results: return outcome signalling empty (sub-agent will downstream emit a "no result" `say` with no `ui`). On error: return outcome signalling failure (Jarvis-side surfaces apologetic speech).
- **`bob.ui_registry`** — Add `MAIL` `UIComponent` to `build_registry()` with `name="Mail"`, full props JSON schema (objects/arrays/enums for flags, ISO date constraint on `receivedAt`, etc.). Schema is canonical reference for both backend validation (`coerce_component_descriptor`) and frontend typing.
- **`backend/prompts/system_chat.md`** (Jarvis) — Append one line in the capabilities section: Bob can fetch emails for the user via a research sub-task. No tool name leaked.
- **Sub-agent system prompt** — Append a paragraph: when the goal is an email lookup, call `gmail_search` with the most specific args you can infer; conclude with `say(speech=meta_summary, ui={component:"Mail", props:…})`; on empty result, call `say(speech="aucun mail trouvé…", ui=null)` and stop.

**New frontend:**

- **`MailOverlay`** — React component mirroring `MarkdownOverlay`'s shell: `.overlay-stage`, `.overlay-card.surface-email`, corner brackets, header (`BOB · SURFACING / INBOX / REF · MAI-xxxx`), body slot, footer actions (`READ ALOUD ↵`, `OPEN ↗`, `DISMISS ESC`). Body block matches `EmailBody` from `Design Mockup/overlay.jsx`: avatar with initials, from name + role + addr + timestamp, flags pills, subject as `h2`, snippet paragraph, attachments chips. Props typed as `MailProps` (the schema's TypeScript mirror). `onClose` callback prop. Esc / backdrop / DISMISS button all close. OPEN button uses `gmailWebUrl` via Tauri's shell-open / `window.open(_, '_blank')` fallback. READ ALOUD wires to a follow-up Jarvis message (deferred — see Out of Scope).
- **`SphereUI`** — Replace direct hardcoded `MarkdownOverlay` rendering with a switch on `ComponentDescriptor.component` (`"Markdown"` → `MarkdownOverlay`, `"Mail"` → `MailOverlay`). Both streaming (`ui_payload` frame) and final (`assistant_msg.ui`) trigger paths share the dispatcher.
- **`frontend/src/types/ws.ts`** — Extend `ComponentDescriptor` typing to a discriminated union: `{component:"Markdown", props:{content:string}}` | `{component:"Mail", props:MailProps}`. `MailProps` mirrors the backend JSON schema.

**Config:**

- **`backend/src/bob/config.py`** — Add `GMAIL_CREDENTIALS_PATH: Path` (default `~/.bob/gmail/credentials.json`) and `GMAIL_TOKEN_PATH: Path` (default `~/.bob/gmail/token.json`). Both env-overridable. No new required env (works out of the box once user bootstraps).

### Contracts

**`gmail_search` tool args (Pydantic):**

- `from_name: str | None` — substring of sender display name; mapped to `from:"…"`.
- `from_email: str | None` — exact or partial sender email; mapped to `from:…`.
- `subject_contains: str | None` — mapped to `subject:…`.
- `after: str | None` — ISO date `YYYY-MM-DD`; mapped to `after:`.
- `before: str | None` — ISO date `YYYY-MM-DD`; mapped to `before:`.
- `has_attachment: bool | None` — mapped to `has:attachment` when true.
- `label: str | None` — Gmail label name; mapped to `label:`.
- `max_results: int = 1` — capped at 5 for MVP safety.

When all fields are null, the tool refuses the call with a validation error.

**`Mail` component props (JSON schema, condensed):**

```
{
  from: { name: string, email: string, role?: string },
  receivedAt: string,             # ISO 8601
  subject: string,
  bodyPreview: string,            # Gmail snippet
  flags: ("priority" | "unread" | "starred")[],
  attachments: [
    { name: string, sizeBytes: number, mime: string }
  ],
  threadId: string,
  messageId: string,
  gmailWebUrl: string
}
```

`from.role` is omitted in MVP (Gmail API does not return it; future enhancement could resolve via People API).

**No new WS frame.** The existing `ui_payload` frame and `assistant_msg.ui` fallback carry `{component:"Mail", props:…}` transparently.

### OAuth & token lifecycle

- One-shot bootstrap: `python -m bob.connectors.gmail.auth` opens browser, user consents, refresh token written to `~/.bob/gmail/token.json` (chmod 0600).
- Runtime: every tool call goes through `auth.get_credentials()` which loads the token, refreshes silently if expired (handled by `google-auth`), and returns a `Credentials`. The refreshed token is persisted back to disk.
- Recovery: if the refresh token is revoked (user changed password, revoked in Google account settings), `get_credentials()` raises `BootstrapRequiredError` with an actionable message; the sub-agent surfaces this as a polite `say("Mon accès à Gmail a expiré, relance le script de connexion.")`.

### Routing & UX flow

1. User: "trouve-moi le dernier mail d'Holyana Callejon".
2. Jarvis: chooses `spawn_task(goal="find the latest email from Holyana Callejon")`, emits a short `say` ack ("Je cherche…").
3. Task row appears in HudTasks panel; sub-agent process starts.
4. Sub-agent: emits a progress reflection ("recherche Gmail"), calls `gmail_search(from_name="Holyana Callejon", max_results=1)`.
5. Tool handler: builds query `from:"Holyana Callejon"`, runs `GmailClient.search_messages`, returns `[EmailMessage]`.
6. Sub-agent: emits another reflection ("lecture du mail"), calls `say(speech="Mail d'Holyana Callejon, sujet '…', reçu …", ui={component:"Mail", props:to_mail_props(msg)})`.
7. Backend: validates props against `Mail` schema, emits `ui_payload` WS frame, then `assistant_msg`, then final `task_completed`.
8. Frontend: `SphereUI` sees `ui_payload` with `component="Mail"`, opens `MailOverlay` with the props. TTS speaks the meta summary in parallel.
9. User: clicks OPEN (browser tab opens Gmail web thread), DISMISS, or Esc.

If `gmail_search` returns 0 results: sub-agent calls `say(speech="Aucun mail récent de Holyana Callejon.", ui=null)`. No overlay opens.

If `gmail_search` raises an error: sub-agent surfaces a polite spoken error, task completes with `failed` state (visible in HudTasks row).

## Testing Decisions

Tests target external behaviour at module boundaries — never internal state, never implementation details of `googleapiclient`. Tests run without network and without a real Gmail account.

**Modules tested:**

- **`bob.connectors.gmail.auth`** — Token loading, refresh, persistence. Fakes `Credentials` with a stub refresh callable. Cases: missing token → `BootstrapRequiredError`; expired token + valid refresh → refresh + persist; expired token + revoked refresh → `BootstrapRequiredError`; valid token → returned unchanged. File-permission check on persist (0600). Prior art: `backend/tests/test_jarvis_store.py` (file-based persistence patterns).
- **`bob.connectors.gmail.client`** — `GmailClient.search_messages` and `get_message`. Mocks `googleapiclient`'s `service.users().messages().list().execute()` and `…get().execute()` to return canned Gmail JSON fixtures. Cases: single result → `EmailMessage` matches expected shape; multiple results → list ordered as returned; empty → `[]`; HTTP error → propagated as connector-level error. Prior art: `backend/tests/test_llm_client.py` (HTTP layer mocking pattern).
- **`bob.connectors.gmail.models`** — `from_gmail_payload` (pure). Fixtures: mail with attachments, mail without, mail with `IMPORTANT`/`STARRED`/`UNREAD` labels, mail with HTML body (snippet still text), mail with non-ASCII subject. `to_mail_props` produces dict that validates against the `Mail` JSON schema. Prior art: `backend/tests/test_ui_registry.py` (schema validation patterns).
- **`bob.connectors.gmail.query_builder`** — Pure function tests on every arg combination. Edge cases: name with quotes, empty all → raises, only `max_results` set → raises.
- **`bob.sub_agent.tool_registry` (gmail_search entry)** — Handler-level test: feeds args, asserts handler calls `GmailClient` with expected query, returns expected outcome. Empty result path. Error path. Prior art: `backend/tests/test_tool_v2_task_surface.py` (tool handler tests).
- **`bob.ui_registry` (MAIL component)** — JSON schema validation on valid + invalid props (missing required field, bad enum in flags, malformed date). Prior art: `backend/tests/test_validation_envelope.py` (schema rejection patterns).
- **Sub-agent runner integration** — End-to-end with mocked `GmailClient` and stub LLM emitting `gmail_search` tool call + `say` with `Mail` UI: sub-agent run produces expected `say.ui` payload. Prior art: `backend/tests/test_sub_agent_v2_runner.py`.
- **Frontend `MailOverlay`** — Vitest + RTL. Renders with full props (mockup-style data) without crashing; renders with empty attachments / empty flags; Esc closes; backdrop click closes; DISMISS click closes; OPEN button calls window-open with `gmailWebUrl`. Prior art: `frontend/src/components/sphere/MarkdownOverlay.test.tsx` (or whatever exists per issue 0026/0031).
- **Frontend `SphereUI` dispatch** — Given a `ui_payload` WS frame with `component="Mail"`, the overlay rendered is `MailOverlay` not `MarkdownOverlay`. Prior art: existing overlay-trigger tests.

**Not tested (out of unit scope):**

- The OAuth installed-app browser flow itself (manual smoke test).
- The exact rendering pixels of `MailOverlay` (no visual regression suite).
- Live Gmail API conformance (relies on Google's API stability + manual smoke after each Google API change).

## Out of Scope

- **Writing mail** (send, reply, forward, draft) — strictly read-only this iteration.
- **Inbox triage actions** (archive, label, mark read, snooze) — requires `gmail.modify` scope, deferred.
- **Multi-result navigation** — `max_results` capped at 5 in args, but MVP UX always shows top 1. Carousel / list overlay deferred.
- **Full HTML body rendering** — body shown as Gmail snippet only. Full plaintext or HTML body rendering is a follow-up (would need iframe sandboxing or a markdown-from-html converter).
- **Attachment download/preview** — attachment chips are metadata-only. Click does nothing (or could fall through to OPEN button in v2).
- **People API contact resolution** — `from_name` is passed verbatim into Gmail's `from:` operator, relying on Gmail's built-in fuzzy contact matching. Exact contact resolution (e.g. "Holyana" → email lookup via People API) is a future enhancement.
- **READ ALOUD button wiring** — the button is rendered in the overlay footer (visual parity with mockup) but its click handler is a no-op (or a "feature coming soon" toast) for MVP. Wiring it to a Jarvis follow-up that reads the body via TTS is a follow-up issue.
- **Shipping a pre-built OAuth client ID** — user is responsible for creating their own GCP project and OAuth client (documented in README). No verified Google app, no published OAuth.
- **Multi-account support** — single account assumed (`accountIndex=0` in `gmailWebUrl`). Multi-account selection deferred.
- **Cache layer** — every `gmail_search` call hits Gmail's API. TTL caching deferred until latency or quota becomes a real concern.
- **Settings UI in Tauri / HUD** — bootstrap is CLI-only for MVP. A "Connectors" tab with a "Connect Gmail" button is a future polish.
- **Other connectors** — Calendar, Drive, Slack, etc. are explicitly excluded. The package layout (`bob.connectors.gmail`) leaves room for them but they are not scoped here.

## Further Notes

- The connector package is named `bob.connectors.gmail` deliberately to leave room for sibling connectors (`bob.connectors.calendar`, `bob.connectors.drive`) without restructuring later.
- The mockup-aligned `Mail` schema includes `from.role`, which Gmail API does not return. The field is left optional in the schema and omitted in MVP. Future work could populate it from People API or from a hand-curated contact directory.
- Jarvis's system prompt update is intentionally vague ("Bob can find emails for the user"). The sub-agent prompt is where the specific tool name and component contract live. This split keeps Jarvis's prompt lean and the sub-agent's prompt context-rich.
- The Gmail web URL pattern `https://mail.google.com/mail/u/{accountIndex}/#inbox/{threadId}` is hardcoded as a single helper in `to_mail_props`. If Google changes the URL scheme, one location updates.
- `from_name` passes through to Gmail's `from:"…"` operator unchanged. Gmail does its own contact matching against display names in the user's inbox. This is good enough for the demo use case ("Holyana Callejon") and avoids a People API dependency in MVP.
- The Mail overlay shares the OverlayCard shell with the markdown overlay (corner brackets, header chips, footer actions). A modest refactor opportunity exists to extract a shared `OverlayShell` component, but is out of scope for this PRD — defer until the third overlay surface lands, per "three lines is better than a premature abstraction."
- Per Bob's voice mode behaviour: the meta summary speech is short and benefits from the existing streaming TTS pipeline. The overlay opens via the existing `ui_payload` channel, which already arrives just after the speech sentence that mentions it — preserving "speak then surface" coherence.
- Security posture: read-only scope, local-only OAuth (no server-side leak surface), restrictive token file perms, no token logging anywhere, no body content sent to debug events (only metadata).
