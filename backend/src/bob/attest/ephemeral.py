"""Isolated, throwaway Bob backend for the attestation harness.

PRD 0016 / issue 0098. :class:`EphemeralBackend` boots a REAL ``uvicorn
bob.main:app`` process on a dedicated free port, pointed at a fresh temp
``BOB_DATA_DIR`` and the deterministic ``fake`` LLM provider, then tears it
down — leaving zero trace on the developer's real state.

Why a subprocess and not an in-process ``TestClient``?
------------------------------------------------------

The harness is a **black-box drive layer over the real WS/HTTP**: it connects
to the running backend exactly like the frontend would (``/ws/chat`` to inject,
``/ws/debug`` to capture) and asserts only on the wire. A dedicated OS port
(issue 0098 acceptance) means a genuine server process. This also guarantees
the harness can later point at an ``external`` backend with the *same* drive
code — only the boot/teardown differs.

Isolation contract (the load-bearing invariant)
------------------------------------------------

Everything that could touch real state is redirected:

- ``BOB_DATA_DIR`` → a fresh ``mkdtemp`` dir (the Jarvis thread DB, the LLM
  selection JSON, the Gmail token all live under it). The real ``~/.bob`` is
  never opened.
- ``BOB_CLEAR_ON_START=true`` → the temp DB starts empty every boot.
- ``ORCHESTRATION_LOG_ENABLED=false`` + ``cwd`` set to the temp dir → no
  ``logs/orchestration.jsonl`` is written into the repo.
- ``LLM_PROVIDER=fake`` + ``BOB_FAKE_LLM_SCRIPT`` → no network LLM call.

A unit test asserts the real ``BOB_DATA_DIR`` is untouched after a full
boot/teardown cycle.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType


def find_free_port() -> int:
    """Reserve and immediately release an ephemeral TCP port on localhost.

    Binding to port 0 lets the OS pick a free port; we read it back, close the
    socket and hand the number to uvicorn. There is a tiny TOCTOU window
    between release and the child binding, negligible for a single-user dev /
    CI harness (and a bind clash surfaces as a clean boot-timeout, not a hang).
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class BackendHandle:
    """Coordinates a running ephemeral backend exposes to the drive layer."""

    host: str
    port: int
    data_dir: Path

    @property
    def http_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_base(self) -> str:
        return f"ws://{self.host}:{self.port}"


class EphemeralBackendError(RuntimeError):
    """Raised when the ephemeral backend fails to boot within the timeout."""


class EphemeralBackend:
    """Boot / teardown a throwaway ``bob.main:app`` on a dedicated port.

    Usage::

        backend = EphemeralBackend(fake_llm_script="[]")
        handle = backend.start()      # blocks until /health is 200
        ...                            # drive it over handle.ws_base / http_base
        backend.stop()                 # SIGTERM, wait, wipe temp dir

    Also a context manager (``with EphemeralBackend(...) as handle:``). The
    process is started with ``start_new_session=True`` so a hard ``stop()`` can
    signal the whole group if the graceful terminate stalls.
    """

    def __init__(
        self,
        *,
        fake_llm_script: str = "",
        fake_stt_transcript: str = "",
        host: str = "127.0.0.1",
        boot_timeout_seconds: float = 30.0,
        python_executable: str | None = None,
    ) -> None:
        self._fake_llm_script = fake_llm_script
        self._fake_stt_transcript = fake_stt_transcript
        self._host = host
        self._boot_timeout = boot_timeout_seconds
        self._python = python_executable or sys.executable
        self._proc: subprocess.Popen[bytes] | None = None
        self._data_dir: Path | None = None
        self._port: int | None = None
        self._handle: BackendHandle | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> BackendHandle:
        """Boot the subprocess and block until ``/health`` answers 200.

        Raises :class:`EphemeralBackendError` if the process dies during boot
        or the health endpoint never comes up within ``boot_timeout_seconds``;
        in both cases the temp dir + any partial process are cleaned up before
        the exception propagates so a failed boot leaks nothing.
        """

        if self._proc is not None:
            raise EphemeralBackendError("EphemeralBackend already started")

        self._data_dir = Path(tempfile.mkdtemp(prefix="bob-attest-"))
        self._port = find_free_port()
        env = self._build_env(self._data_dir, self._port)

        argv = [
            self._python,
            "-m",
            "uvicorn",
            "bob.main:app",
            "--host",
            self._host,
            "--port",
            str(self._port),
            "--log-level",
            "warning",
            # The ``/ws/debug`` capture socket keeps a server-side handler
            # parked in an infinite ``subscribe()`` loop; without a short
            # graceful cap uvicorn waits its full default (~10s) for that task
            # to drain on SIGTERM, dominating the run's duration. 2s is plenty
            # for the real request handlers to finish.
            "--timeout-graceful-shutdown",
            "2",
        ]
        try:
            self._proc = subprocess.Popen(
                argv,
                env=env,
                # Run from the temp dir so any relative-path side effect (e.g.
                # ``logs/orchestration.jsonl`` when the sink is enabled) lands
                # in the throwaway dir, never the repo / cwd.
                cwd=str(self._data_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._wait_until_healthy()
        except Exception:
            # Boot failed — never leak the process or the temp dir.
            self.stop()
            raise

        self._handle = BackendHandle(host=self._host, port=self._port, data_dir=self._data_dir)
        return self._handle

    def stop(self) -> None:
        """Terminate the subprocess and wipe the temp data dir. Idempotent."""

        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                # Graceful terminate stalled — kill the whole session group.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), signal_sigkill())
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5.0)
        self._proc = None

        if self._data_dir is not None:
            shutil.rmtree(self._data_dir, ignore_errors=True)
            self._data_dir = None
        self._handle = None

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> BackendHandle:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # -- internals ----------------------------------------------------------

    def _build_env(self, data_dir: Path, port: int) -> dict[str, str]:
        """Curate the subprocess env so it is isolated + deterministic.

        Starts from the parent env (so ``PATH`` / the uv venv resolve) and
        overrides the Bob knobs. The ``fake`` provider needs no ``LLM_*``
        values, but the parent shell may carry a real ``LLM_PROVIDER`` — we
        force ``fake`` and a fresh data dir regardless.
        """

        env = dict(os.environ)
        env.update(
            {
                "BOB_DATA_DIR": str(data_dir),
                "BOB_CLEAR_ON_START": "true",
                "ORCHESTRATION_LOG_ENABLED": "false",
                "LLM_PROVIDER": "fake",
                "BOB_FAKE_LLM_SCRIPT": self._fake_llm_script,
                # Deterministic STT for the harness: the fake engine converges
                # to ``BOB_FAKE_STT_TRANSCRIPT`` so an ``--audio`` scenario needs
                # no native whisper model. Harmless for text-only scenarios (no
                # voice turn is opened).
                "STT_ENGINE": "fake",
                "BOB_FAKE_STT_TRANSCRIPT": self._fake_stt_transcript,
                "BACKEND_HOST": self._host,
                "BACKEND_PORT": str(port),
                # Text-only harness: skip the Kokoro download + espeak-ng warmup
                # so boot is offline + fast and cannot native-abort in CI.
                "BOB_SKIP_TTS_PRELOAD": "true",
                # Deterministic, native-free TTS for the harness (PRD 0016 /
                # issue 0100): the ``fake`` engine yields fixed silent PCM
                # chunks so an ``--audio`` full-duplex scenario can attest the
                # audio-out path (``audio_chunk`` events → FSM ``bob_speaking``)
                # with zero dependency on espeak-ng / torch. Harmless for
                # text-only scenarios (no synthesis is requested).
                "TTS_ENGINE": "fake",
                # Keep Gmail / Tavily / MCP dormant — point their on-disk paths
                # into the throwaway dir and leave keys unset so nothing reaches
                # out and nothing touches the real ``~/.bob``.
                "GMAIL_CREDENTIALS_PATH": str(data_dir / "gmail" / "credentials.json"),
                "GMAIL_TOKEN_PATH": str(data_dir / "gmail" / "token.json"),
            }
        )
        # A real ``.env`` next to the repo would otherwise re-seed LLM_* — the
        # process-level env wins (pydantic-settings precedence), so forcing the
        # keys above is enough; we additionally clear any inherited per-role
        # backend override so it can't pull a non-fake client.
        env.pop("JARVIS_BACKEND", None)
        env.pop("SUBAGENT_BACKEND", None)
        return env

    def _wait_until_healthy(self) -> None:
        """Poll ``GET /health`` until 200 or the boot timeout elapses."""

        url = f"http://{self._host}:{self._port}/health"
        deadline = time.monotonic() + self._boot_timeout
        last_error: str = "no attempt made"
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise EphemeralBackendError(
                    f"backend process exited during boot (code={proc.returncode})"
                )
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        return
                    last_error = f"health returned {resp.status}"
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_error = repr(exc)
            time.sleep(0.1)
        raise EphemeralBackendError(
            f"backend did not become healthy within {self._boot_timeout}s "
            f"(last error: {last_error})"
        )


def signal_sigkill() -> int:
    """Return ``signal.SIGKILL`` (split out so the import stays POSIX-guarded).

    ``os.killpg`` + ``SIGKILL`` are POSIX-only; the harness targets macOS /
    Linux dev + CI. Importing :mod:`signal` lazily here keeps the module
    importable on platforms lacking these symbols (mypy/lint on any OS).
    """

    import signal

    return int(signal.SIGKILL)
