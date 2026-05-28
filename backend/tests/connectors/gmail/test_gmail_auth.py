"""Tests for :mod:`bob.connectors.gmail.auth`.

We exercise the runtime path (:func:`get_credentials`) against stubbed
``Credentials`` objects so the tests run without any network or browser
interaction. The bootstrap CLI path (:func:`run_bootstrap`) is exercised
by replacing :class:`InstalledAppFlow` with a fake that returns a stubbed
credentials object.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bob.config import get_settings
from bob.connectors.gmail import auth as gmail_auth
from bob.connectors.gmail.auth import (
    BootstrapRequiredError,
    MissingCredentialsError,
    RefreshFailedError,
)


@pytest.fixture()
def gmail_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Path]]:
    """Point the auth module at a tmp credentials/token pair.

    We patch the module-level helpers rather than the env so we do not
    have to invalidate the cached :func:`get_settings` instance for every
    test. Also clears the lru_cache once so the override stays sticky if
    a test imports :class:`Settings` directly.
    """

    creds = tmp_path / "credentials.json"
    token = tmp_path / "token.json"

    monkeypatch.setattr(gmail_auth, "_credentials_path", lambda: creds)
    monkeypatch.setattr(gmail_auth, "_token_path", lambda: token)

    # The runtime token path is also used by `_main` for its success
    # message; tests of `_main` use the same monkeypatched value.
    get_settings.cache_clear()
    yield {"credentials": creds, "token": token}
    get_settings.cache_clear()


class _FakeCreds:
    """Stand-in for :class:`google.oauth2.credentials.Credentials`.

    Implements the surface :mod:`bob.connectors.gmail.auth` touches: the
    ``valid`` / ``expired`` / ``refresh_token`` flags, the ``refresh()``
    method, and ``to_json()`` for persistence.
    """

    def __init__(
        self,
        *,
        valid: bool = True,
        expired: bool = False,
        refresh_token: str | None = "rtok-1",
        refresh_raises: BaseException | None = None,
        new_token: str = "fresh-token",
    ) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.token = "atok-1"
        self._refresh_raises = refresh_raises
        self._new_token = new_token
        self.refresh_called = False

    def refresh(self, request: Any) -> None:
        self.refresh_called = True
        if self._refresh_raises is not None:
            raise self._refresh_raises
        # Mimic google-auth's behaviour: refreshing flips the validity bits.
        self.valid = True
        self.expired = False
        self.token = self._new_token

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "scopes": list(gmail_auth.SCOPES),
            }
        )


def _install_loader(monkeypatch: pytest.MonkeyPatch, creds: _FakeCreds) -> None:
    """Make :func:`gmail_auth._load_credentials_from_file` return ``creds``."""

    def _loader(path: Path) -> _FakeCreds:
        return creds

    monkeypatch.setattr(gmail_auth, "_load_credentials_from_file", _loader)


# --- get_credentials ----------------------------------------------------------


def test_get_credentials_missing_token_raises_bootstrap_required(
    gmail_paths: dict[str, Path],
) -> None:
    with pytest.raises(BootstrapRequiredError, match="No Gmail token"):
        gmail_auth.get_credentials()


def test_get_credentials_valid_token_returns_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    fake = _FakeCreds(valid=True, expired=False)
    _install_loader(monkeypatch, fake)
    # The file just needs to exist; loader is stubbed.
    gmail_paths["token"].write_text("{}", encoding="utf-8")

    result = gmail_auth.get_credentials()

    # ``is fake`` — explicit identity check; cast keeps mypy happy because
    # the function's return type is the Protocol, not _FakeCreds.
    assert result is fake  # type: ignore[comparison-overlap]
    assert fake.refresh_called is False


def test_get_credentials_expired_with_refresh_token_refreshes_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    fake = _FakeCreds(
        valid=False,
        expired=True,
        refresh_token="rtok-1",
        new_token="fresh-token-xyz",
    )
    _install_loader(monkeypatch, fake)
    gmail_paths["token"].write_text("stale", encoding="utf-8")

    result = gmail_auth.get_credentials()

    assert result is fake  # type: ignore[comparison-overlap]
    assert fake.refresh_called is True
    # Refreshed token persisted with restrictive perms.
    persisted = json.loads(gmail_paths["token"].read_text(encoding="utf-8"))
    assert persisted["token"] == "fresh-token-xyz"
    mode = stat.S_IMODE(os.stat(gmail_paths["token"]).st_mode)
    assert mode == 0o600


def test_get_credentials_revoked_refresh_token_raises_bootstrap_required(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    from google.auth.exceptions import RefreshError

    fake = _FakeCreds(
        valid=False,
        expired=True,
        refresh_token="rtok-1",
        refresh_raises=RefreshError("invalid_grant"),  # type: ignore[no-untyped-call]
    )
    _install_loader(monkeypatch, fake)
    gmail_paths["token"].write_text("stale", encoding="utf-8")

    with pytest.raises(BootstrapRequiredError, match="rejected"):
        gmail_auth.get_credentials()


def test_get_credentials_no_refresh_token_raises_bootstrap_required(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    fake = _FakeCreds(valid=False, expired=True, refresh_token=None)
    _install_loader(monkeypatch, fake)
    gmail_paths["token"].write_text("partial", encoding="utf-8")

    with pytest.raises(BootstrapRequiredError, match="unusable"):
        gmail_auth.get_credentials()


def test_get_credentials_transient_refresh_failure_raises_refresh_failed(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    fake = _FakeCreds(
        valid=False,
        expired=True,
        refresh_token="rtok-1",
        refresh_raises=ConnectionError("network down"),
    )
    _install_loader(monkeypatch, fake)
    gmail_paths["token"].write_text("stale", encoding="utf-8")

    with pytest.raises(RefreshFailedError, match="Failed to refresh"):
        gmail_auth.get_credentials()


def test_get_credentials_corrupt_token_file_raises_refresh_failed(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    def _bad_loader(path: Path) -> Any:
        raise ValueError("corrupt token")

    monkeypatch.setattr(gmail_auth, "_load_credentials_from_file", _bad_loader)
    gmail_paths["token"].write_text("garbage", encoding="utf-8")

    with pytest.raises(RefreshFailedError, match="Failed to load"):
        gmail_auth.get_credentials()


# --- write permissions --------------------------------------------------------


def test_write_token_file_creates_with_0600(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "token.json"
    gmail_auth._write_token_file(target, '{"k":"v"}')

    assert target.read_text(encoding="utf-8") == '{"k":"v"}'
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600


def test_write_token_file_chmods_existing_file_to_0600(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    target.write_text("loose", encoding="utf-8")
    target.chmod(0o644)

    gmail_auth._write_token_file(target, "tight")

    assert target.read_text(encoding="utf-8") == "tight"
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600


# --- run_bootstrap ------------------------------------------------------------


def test_run_bootstrap_missing_credentials_raises(
    gmail_paths: dict[str, Path],
) -> None:
    with pytest.raises(MissingCredentialsError, match="not found"):
        gmail_auth.run_bootstrap()


def test_run_bootstrap_runs_flow_and_persists_token(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    gmail_paths["credentials"].write_text("{}", encoding="utf-8")

    captured: dict[str, Any] = {}

    class _FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path: str, scopes: list[str]) -> _FakeFlow:
            captured["path"] = path
            captured["scopes"] = list(scopes)
            return cls()

        def run_local_server(self, port: int) -> _FakeCreds:
            captured["port"] = port
            return _FakeCreds(valid=True, expired=False, new_token="bootstrap-tok")

    # Inject the fake into the lazy-imported module.
    import google_auth_oauthlib.flow as oauth_flow

    monkeypatch.setattr(oauth_flow, "InstalledAppFlow", _FakeFlow)

    gmail_auth.run_bootstrap()

    assert captured["path"] == str(gmail_paths["credentials"])
    assert captured["scopes"] == list(gmail_auth.SCOPES)
    assert captured["port"] == 0
    persisted = json.loads(gmail_paths["token"].read_text(encoding="utf-8"))
    assert persisted["token"] == "atok-1"  # _FakeCreds default; to_json
    mode = stat.S_IMODE(os.stat(gmail_paths["token"]).st_mode)
    assert mode == 0o600


def test_module_cli_invokes_run_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    called: dict[str, bool] = {}

    def _fake_bootstrap() -> None:
        called["yes"] = True

    monkeypatch.setattr(gmail_auth, "run_bootstrap", _fake_bootstrap)
    assert gmail_auth._main() == 0
    assert called["yes"] is True


def test_module_cli_returns_nonzero_on_error(
    monkeypatch: pytest.MonkeyPatch,
    gmail_paths: dict[str, Path],
) -> None:
    def _raise() -> None:
        raise MissingCredentialsError("nope")

    monkeypatch.setattr(gmail_auth, "run_bootstrap", _raise)
    assert gmail_auth._main() == 1
