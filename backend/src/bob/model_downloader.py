"""Kokoro TTS model artifact bootstrap.

Ensures the ONNX model + voices pack are present on disk before
:mod:`bob.tts_service` loads them. Files are kept under
``KOKORO_MODEL_DIR`` (default ``~/.bob/models/kokoro/``). Missing files
are streamed down from the URLs configured in :mod:`bob.config`, with
progress logged via ``structlog`` every ~5%.

The downloader is intentionally dependency-light — plain ``urllib`` so
we don't pull a fat HTTP client just for two one-off fetches. It writes
to a ``.part`` sibling and atomically renames on success, so an aborted
download won't leave a half-written file that the model loader would
then try to mmap.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path

import structlog

from bob.config import Settings, get_settings

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class KokoroPaths:
    """Resolved on-disk locations for the Kokoro artifacts."""

    model_path: Path
    voices_path: Path


def _download(url: str, destination: Path) -> None:
    """Stream ``url`` to ``destination`` with periodic progress logs."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    _logger.info("kokoro.download.start", url=url, destination=str(destination))

    with urllib.request.urlopen(url) as response:
        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header and total_header.isdigit() else 0
        downloaded = 0
        next_log_pct = 5
        chunk_size = 1 << 20  # 1 MiB
        with tmp.open("wb") as out:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100 // total
                    if pct >= next_log_pct:
                        _logger.info(
                            "kokoro.download.progress",
                            destination=destination.name,
                            percent=pct,
                            downloaded_bytes=downloaded,
                            total_bytes=total,
                        )
                        next_log_pct = pct + 5

    tmp.replace(destination)
    _logger.info(
        "kokoro.download.done",
        destination=str(destination),
        size_bytes=destination.stat().st_size,
    )


def ensure_kokoro_ready(settings: Settings | None = None) -> KokoroPaths:
    """Return on-disk paths to the Kokoro model + voices, downloading if absent.

    Idempotent: existing files are kept as-is. Safe to call repeatedly
    (e.g. on app startup and again on the first synthesis request).
    """

    settings = settings or get_settings()
    model_dir = Path(settings.KOKORO_MODEL_DIR)
    model_path = model_dir / settings.KOKORO_MODEL_FILENAME
    voices_path = model_dir / settings.KOKORO_VOICES_FILENAME

    if not model_path.exists():
        _download(settings.KOKORO_MODEL_URL, model_path)
    else:
        _logger.debug("kokoro.model.present", path=str(model_path))

    if not voices_path.exists():
        _download(settings.KOKORO_VOICES_URL, voices_path)
    else:
        _logger.debug("kokoro.voices.present", path=str(voices_path))

    return KokoroPaths(model_path=model_path, voices_path=voices_path)
