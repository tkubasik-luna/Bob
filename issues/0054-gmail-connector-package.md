## Parent

`prd/0007-gmail-mail-overlay.md`

## What to build

Build the Gmail connector as a standalone `bob.connectors.gmail` package that can authenticate, query Gmail, and translate results to domain objects — independent of any tool wiring or UI.

`auth` module: OAuth2 installed-app flow (`google-auth-oauthlib`) with two public entry points. Runtime path: `get_credentials() -> Credentials` loads the cached token from `GMAIL_TOKEN_PATH`, refreshes silently if expired (via `google-auth` refresh), persists refreshed token back with chmod 0600, raises `BootstrapRequiredError` with actionable message if the token is missing or the refresh token is revoked. CLI bootstrap: `run_bootstrap()` (also reachable via `python -m bob.connectors.gmail.auth`) loads `credentials.json` from `GMAIL_CREDENTIALS_PATH`, runs the installed-app flow on localhost, writes the token JSON with restrictive perms. Error taxonomy: `MissingCredentialsError`, `BootstrapRequiredError`, `RefreshFailedError`.

`client` module: `GmailClient(credentials: Credentials)` with `search_messages(query: str, max_results: int = 1) -> list[EmailMessage]` and `get_message(message_id: str) -> EmailMessage`. Wraps `googleapiclient.discovery.build("gmail","v1",credentials=…)`. Translates the Gmail API JSON to `EmailMessage` via `models.from_gmail_payload`.

`models` module: `EmailMessage` dataclass (`id`, `thread_id`, `from_name`, `from_email`, `received_at` datetime, `subject`, `snippet`, `labels: list[str]`, `attachments: list[Attachment]`). `Attachment` dataclass (`filename`, `size_bytes`, `mime_type`, `attachment_id`). Pure factory `from_gmail_payload(payload: dict) -> EmailMessage` (canned-fixture testable). Adapter `to_mail_props(msg: EmailMessage, account_index: int = 0) -> dict` produces the `Mail` component props dict, deriving `flags` from labels (`IMPORTANT` → `priority`, `UNREAD` → `unread`, `STARRED` → `starred`) and pre-building `gmailWebUrl` as `https://mail.google.com/mail/u/{account_index}/#inbox/{thread_id}`.

`query_builder` module (or inline pure function): `build_query(from_name=None, from_email=None, subject_contains=None, after=None, before=None, has_attachment=None, label=None) -> str` mapping structured args to Gmail search syntax (`from:"…"`, `subject:…`, `after:YYYY-MM-DD`, `before:YYYY-MM-DD`, `has:attachment`, `label:…`). Raises if all args are None.

Config: add `GMAIL_CREDENTIALS_PATH: Path` (default `~/.bob/gmail/credentials.json`) and `GMAIL_TOKEN_PATH: Path` (default `~/.bob/gmail/token.json`) to `backend/src/bob/config.py`, env-overridable.

Dependencies: add `google-auth`, `google-auth-oauthlib`, `google-api-python-client` to `backend/pyproject.toml`.

Manual smoke verification (HITL): user creates a GCP project with OAuth client (Desktop type), downloads `credentials.json` to `~/.bob/gmail/`, runs `python -m bob.connectors.gmail.auth`, consents in browser, then runs a small dev script that instantiates `GmailClient(auth.get_credentials())` and prints subject lines for the 3 most recent messages.

Document the GCP project setup step-by-step in README (project creation, Gmail API enable, OAuth consent screen, OAuth client creation, credentials download, file placement, bootstrap script run).

## Acceptance criteria

- [ ] `bob.connectors.gmail` package created with `auth`, `client`, `models` modules (plus optional `query_builder`).
- [ ] `auth.get_credentials()` handles: missing token → `BootstrapRequiredError`; valid token → return; expired + refreshable → refresh + persist; refresh revoked → `BootstrapRequiredError`.
- [ ] `auth.run_bootstrap()` runs installed-app flow, persists token with chmod 0600; reachable via `python -m bob.connectors.gmail.auth`.
- [ ] `GmailClient.search_messages` and `get_message` return `EmailMessage` lists/instances; internal Gmail JSON shape never leaks past the client.
- [ ] `EmailMessage` and `Attachment` dataclasses defined; `from_gmail_payload` is a pure function; `to_mail_props` derives flags from labels and builds `gmailWebUrl`.
- [ ] `query_builder` maps all eight args to Gmail search syntax; raises on all-None args.
- [ ] `GMAIL_CREDENTIALS_PATH` and `GMAIL_TOKEN_PATH` configurable via env, defaulting under `~/.bob/gmail/`.
- [ ] `google-auth`, `google-auth-oauthlib`, `google-api-python-client` added to dependencies.
- [ ] Unit tests — auth: missing/valid/expired-refreshable/revoked cases with stubbed `Credentials`; persist file is chmod 0600.
- [ ] Unit tests — client: `search_messages` and `get_message` against canned Gmail JSON fixtures, mocked at the `googleapiclient` HTTP transport layer (not patching internals).
- [ ] Unit tests — models: `from_gmail_payload` on fixtures (with/without attachments, with IMPORTANT/STARRED/UNREAD labels, non-ASCII subject); `to_mail_props` output validates against the backend `Mail` JSON schema.
- [ ] Unit tests — query_builder: every arg combination + escape edge cases (name with quotes); all-None raises.
- [ ] README documents GCP project setup + bootstrap script run.
- [ ] Manual smoke: bootstrap script + dev test script print 3 recent inbox subjects (HITL).

## Blocked by

None - can start immediately.
