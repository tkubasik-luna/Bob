## Parent

`prd/0007-gmail-mail-overlay.md`

## What to build

Polish the failure and empty-result branches of the Gmail search flow so the user never sees a broken state and the HUD never opens an empty Mail overlay.

Empty result (Gmail returned zero matches): the sub-agent emits `say(speech="Aucun mail récent de {sender}.", ui=null)` and stops. No `ui_payload` frame, no overlay opens. Task transitions to `done` with no result payload — HudTasks row reflects this state.

OAuth refresh failure (token revoked, refresh exchange rejected): the connector's `auth.get_credentials()` raises `BootstrapRequiredError` with the user-facing recovery instructions. The `gmail_search` handler catches it and returns a structured tool error (`error_code="gmail_auth_expired"`). The sub-agent surfaces this as `say(speech="Mon accès à Gmail a expiré — relance le script de connexion (python -m bob.connectors.gmail.auth).", ui=null)` and marks the task `failed`.

Network / API error (Gmail API unreachable, HTTP 5xx, quota exhausted): the handler catches the `googleapiclient` error and returns a structured tool error (`error_code="gmail_api_unreachable"`). Sub-agent surfaces a generic polite spoken error ("Je n'ai pas pu joindre Gmail à l'instant — réessaie dans un moment."). Task marked `failed`.

No-arg call (LLM hallucinates an empty `gmail_search()` call): handler-level Pydantic validation already rejects all-None args; ensure the resulting dispatcher error surfaces as a sub-agent retry rather than crashing the task (per existing validation retry policy).

Body-leak hygiene: ensure mail snippet / subject content never lands in debug event payloads or logs at INFO level. Only metadata (message id, thread id, sender email, label set) is loggable.

## Acceptance criteria

- [ ] Empty result branch: sub-agent test with mocked `GmailClient` returning `[]` produces `say.ui=null` and no `ui_payload` WS frame; task ends in `done` state with no result payload.
- [ ] OAuth refresh failure branch: `auth.get_credentials()` patched to raise `BootstrapRequiredError`; handler returns structured error; sub-agent emits the recovery-instruction speech; task marked `failed`.
- [ ] Network / API error branch: `GmailClient.search_messages` patched to raise a Google API HttpError; handler returns structured error; sub-agent emits generic polite speech; task marked `failed`.
- [ ] Validation branch: `gmail_search` called with all-None args fails validation cleanly; existing retry policy applies; no crash.
- [ ] Privacy: log + debug event review confirms mail subject and snippet bodies never appear in INFO-level logs or `DebugEvent` payloads; only message id / thread id / sender email / label set may be logged.
- [ ] Unit tests for each of the four branches (empty, auth failure, API failure, validation failure).
- [ ] Manual smoke: query for a non-existent sender returns the empty-result speech with no overlay; revoking the OAuth refresh token (Google account settings) and re-running the query surfaces the bootstrap-required speech.

## Blocked by

`issues/0055-gmail-search-tool-e2e.md`
