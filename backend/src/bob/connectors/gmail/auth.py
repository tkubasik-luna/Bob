"""OAuth2 installed-app flow for Gmail (read-only scope).

Two public entry points:

- :func:`get_credentials` — runtime path. Load the cached token from
  :attr:`bob.config.Settings.GMAIL_TOKEN_PATH`, refresh silently if
  expired, persist the refreshed token back with ``chmod 0600``, raise
  :class:`BootstrapRequiredError` with an actionable message if the token
  is missing or the refresh token is revoked.
- :func:`run_bootstrap` — one-shot CLI. Load ``credentials.json`` from
  :attr:`bob.config.Settings.GMAIL_CREDENTIALS_PATH`, run the installed-app
  flow on localhost (Google's `InstalledAppFlow.run_local_server`), persist
  the resulting token with restrictive permissions.

The module is also a ``python -m`` entry: ``python -m
bob.connectors.gmail.auth`` calls :func:`run_bootstrap`. The runtime + CLI
paths share the same token-file format / paths so the CLI's output is
directly consumed by the next call to :func:`get_credentials`.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any, Protocol

from bob.config import get_settings

# Read-only Gmail scope — the worst-case blast radius is "read", never
# "delete" or "send" (PRD 0007, user story #12).
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)


# --- Error taxonomy -----------------------------------------------------------


class GmailAuthError(Exception):
    """Base class for Gmail auth failures."""


class MissingCredentialsError(GmailAuthError):
    """The OAuth ``credentials.json`` file is missing from disk.

    Raised by :func:`run_bootstrap` when the user has not yet downloaded
    their OAuth client credentials from the GCP console. The error message
    points at the configured path so the user knows where to drop the file.
    """


class BootstrapRequiredError(GmailAuthError):
    """The runtime token is missing or its refresh token is no longer valid.

    Raised by :func:`get_credentials` when either:

    - No ``token.json`` exists yet (first run, or the file was deleted).
    - The cached token has expired and the refresh attempt failed because
      the refresh token itself was revoked (e.g. user de-authorised the
      app from their Google account settings).

    Recovery in both cases is the same: re-run ``python -m
    bob.connectors.gmail.auth`` to interactively consent and persist a
    fresh token.
    """


class RefreshFailedError(GmailAuthError):
    """The token refresh attempt failed for a non-revocation reason.

    Network errors, Google-side outages, malformed cached token, etc.
    Distinct from :class:`BootstrapRequiredError` because the user does
    *not* need to re-consent; retrying should eventually succeed. The
    caller (sub-agent tool handler) can decide whether to surface this as
    a transient failure or escalate.
    """


# --- Internal helpers ---------------------------------------------------------


class _RefreshableCredentials(Protocol):
    """The subset of :class:`google.oauth2.credentials.Credentials` we touch.

    Defined as a :class:`typing.Protocol` so tests can stub the surface
    without importing the heavy ``google-auth`` package, and so mypy stays
    strict even when the third-party stubs are missing.
    """

    expired: bool
    valid: bool
    refresh_token: str | None
    token: str | None

    def refresh(self, request: Any) -> None: ...

    def to_json(self) -> str: ...


def _token_path() -> Path:
    return Path(get_settings().GMAIL_TOKEN_PATH).expanduser()


def _credentials_path() -> Path:
    return Path(get_settings().GMAIL_CREDENTIALS_PATH).expanduser()


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_token_file(path: Path, payload: str) -> None:
    """Persist ``payload`` (JSON string) with ``chmod 0600`` on the result.

    Uses ``os.open`` with a restrictive mode for the create case so the
    file never briefly exists with the default umask permissions; an
    explicit ``chmod`` follows so callers can re-persist into an existing
    file (e.g. a refreshed token) without losing the 0600 guarantee.
    """

    _ensure_parent_dir(path)
    # Create-or-truncate with mode 0600. Pass the descriptor into a binary
    # file object so we can write bytes (the encoded JSON).
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload.encode("utf-8"))
    except Exception:
        # If fdopen succeeded the with-block closed the fd; if it failed
        # before taking ownership we close it explicitly to avoid leaks.
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    # Explicit chmod for the overwrite-existing case (the O_CREAT mode is
    # ignored when the file already existed with different perms).
    os.chmod(path, 0o600)


def _load_credentials_from_file(path: Path) -> _RefreshableCredentials:
    """Hydrate a :class:`google.oauth2.credentials.Credentials` from disk.

    Imported lazily so the heavy google-auth packages aren't dragged into
    every test that touches :mod:`bob.connectors.gmail` for an unrelated
    reason (e.g. a models-only test).
    """

    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call,no-any-return]
        str(path), list(SCOPES)
    )


# --- Public API ---------------------------------------------------------------


def get_credentials() -> _RefreshableCredentials:
    """Return refreshed Gmail credentials, or raise an actionable error.

    The token is loaded from :attr:`Settings.GMAIL_TOKEN_PATH`; if absent,
    :class:`BootstrapRequiredError` is raised pointing the user at
    ``python -m bob.connectors.gmail.auth``. If the cached token is
    expired and a refresh token is present, we attempt a silent refresh
    via google-auth and persist the refreshed token back with chmod 0600.
    A revoked refresh token surfaces as :class:`BootstrapRequiredError`
    (re-consent required); other refresh failures surface as
    :class:`RefreshFailedError` (transient).
    """

    token_path = _token_path()
    if not token_path.exists():
        raise BootstrapRequiredError(
            "No Gmail token found at "
            f"{token_path}. Run `python -m bob.connectors.gmail.auth` to consent."
        )

    try:
        creds = _load_credentials_from_file(token_path)
    except Exception as exc:
        raise RefreshFailedError(
            f"Failed to load cached Gmail token at {token_path}: {exc}"
        ) from exc

    if creds.valid:
        return creds

    if not creds.expired or not creds.refresh_token:
        # No refresh token (would only happen on a malformed token.json)
        # or not expired but still not valid — both warrant a re-bootstrap
        # rather than a refresh.
        raise BootstrapRequiredError(
            "Cached Gmail token is unusable (no refresh token or revoked). "
            "Re-run `python -m bob.connectors.gmail.auth`."
        )

    # Expired + refreshable: try a silent refresh.
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        # Refresh token revoked or otherwise rejected by Google — user
        # must re-consent. Distinct error so the caller can surface a
        # different message for "re-auth needed" vs "try again later".
        raise BootstrapRequiredError(
            "Gmail refresh token rejected by Google "
            f"(probably revoked): {exc}. "
            "Re-run `python -m bob.connectors.gmail.auth`."
        ) from exc
    except Exception as exc:
        raise RefreshFailedError(f"Failed to refresh Gmail credentials: {exc}") from exc

    # Persist the refreshed token back so subsequent calls reuse the
    # latest access token instead of re-refreshing every time.
    try:
        _write_token_file(token_path, creds.to_json())
    except OSError as exc:
        # We have valid credentials in-memory; persistence failure is
        # logged-and-degraded but does not invalidate the returned creds.
        # We surface it via RefreshFailedError so the next call retries
        # cleanly rather than silently keeping a stale on-disk token.
        raise RefreshFailedError(
            f"Refreshed Gmail credentials but failed to persist at {token_path}: {exc}"
        ) from exc

    return creds


def run_bootstrap() -> None:
    """Interactive OAuth consent flow — persists a fresh ``token.json``.

    Loads the GCP-issued OAuth client from
    :attr:`Settings.GMAIL_CREDENTIALS_PATH`, runs
    :meth:`InstalledAppFlow.run_local_server` (opens the user's default
    browser to Google's consent screen), and writes the resulting token
    JSON to :attr:`Settings.GMAIL_TOKEN_PATH` with ``chmod 0600``.

    Idempotent: calling it twice produces two valid (but distinct)
    refresh tokens; only the latest is persisted on disk.
    """

    credentials_path = _credentials_path()
    if not credentials_path.exists():
        raise MissingCredentialsError(
            "OAuth client credentials not found at "
            f"{credentials_path}. Download `credentials.json` from your "
            "GCP project (Desktop OAuth client) and place it at that path. "
            "See the README's 'Gmail connector' section for the setup steps."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_path),
        list(SCOPES),
    )
    creds = flow.run_local_server(port=0)

    token_path = _token_path()
    _write_token_file(token_path, creds.to_json())


def _main() -> int:
    """Module CLI: ``python -m bob.connectors.gmail.auth``."""

    try:
        run_bootstrap()
    except GmailAuthError as exc:
        print(f"gmail bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(f"gmail bootstrap ok — token persisted at {_token_path()}")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(_main())


__all__ = [
    "SCOPES",
    "BootstrapRequiredError",
    "GmailAuthError",
    "MissingCredentialsError",
    "RefreshFailedError",
    "get_credentials",
    "run_bootstrap",
]
