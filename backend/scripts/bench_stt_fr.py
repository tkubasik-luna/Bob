#!/usr/bin/env python3
"""Bench French STT: sherpa-onnx streaming transducer vs whisper.cpp large-v3-turbo.

Decision gate for the « true-streaming transducer » STT upgrade (see the
SherpaSttEngine design). The architectural win of a transducer (token-by-token,
~200-300 ms latency, no trailing-buffer re-decode) is only worth taking if the
*French* model holds up on accuracy — and the only ready FR streaming transducer
in sherpa-onnx is the 2023 zipformer, which the upstream issue tracker flags as
error-prone. This script measures that head-to-head so we don't code the engine
blind.

What it does, per input clip:
  1. Reads a 16 kHz mono s16le WAV (the same contract the live mic path uses).
  2. Transcribes it with whisper.cpp (reuses the production WhisperCppSttEngine).
  3. Transcribes it with sherpa-onnx, fed in small chunks to mimic the live
     streaming path, and reports the final settled transcript.
  4. Times both → real-time factor (RTF = process_time / audio_duration).
  5. If a reference transcript is supplied (--ref-dir / sidecar .txt), computes
     word error rate (WER) for each engine against it.

This is a throwaway bench, NOT shipped wiring — it lives under scripts/ and is
not imported by the package. It only needs the optional `stt` extra (whisper)
and `sherpa-onnx` installed in the venv:

    uv sync --extra stt
    uv pip install sherpa-onnx

Get the French model (≈ run once):

    python scripts/bench_stt_fr.py --download-fr-model models/

Run the bench:

    python scripts/bench_stt_fr.py \
        --sherpa-model-dir models/sherpa-onnx-streaming-zipformer-fr-2023-04-14 \
        --wav-dir ~/bob-voice-clips \
        --ref-dir ~/bob-voice-clips        # optional: <clip>.txt holds the truth

If you have no labelled clips, omit --ref-dir: you still get both transcripts
side by side (eyeball the FR quality) plus RTF.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Production sample-rate / frame contract (Annexe A.1).
SAMPLE_RATE = 16_000
# Feed sherpa in 100 ms chunks to mimic the live mic cadence (frames arrive
# ~20-40 ms apart; 100 ms keeps the decode loop honest without per-frame noise).
CHUNK_SECONDS = 0.1

FR_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-streaming-zipformer-fr-2023-04-14.tar.bz2"
)


# --- audio io ----------------------------------------------------------------


def read_wav_pcm(path: Path) -> bytes:
    """Read a 16 kHz mono s16le WAV, return raw PCM bytes. Hard-fail otherwise.

    We do NOT resample here on purpose: the live path ships exactly 16 kHz mono
    s16le, so the bench must measure that same input. Convert your clips up front
    (e.g. ``ffmpeg -i in.m4a -ar 16000 -ac 1 -sample_fmt s16 out.wav``).
    """

    with wave.open(str(path), "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if (ch, width, rate) != (1, 2, SAMPLE_RATE):
            raise ValueError(
                f"{path.name}: need mono/16-bit/16kHz, got "
                f"channels={ch} width={width * 8}-bit rate={rate}. "
                f"Reconvert: ffmpeg -i {path.name} -ar 16000 -ac 1 -sample_fmt s16 out.wav"
            )
        return w.readframes(n)


def pcm_to_float32(pcm: bytes) -> np.ndarray:
    """s16le bytes → float32 [-1, 1] (what both engines consume)."""

    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


# --- WER ---------------------------------------------------------------------


def _normalize(text: str) -> list[str]:
    """Lowercase, strip punctuation to spaces, split on whitespace."""

    keep = []
    for c in text.lower():
        keep.append(c if c.isalnum() or c.isspace() else " ")
    return "".join(keep).split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein word distance / reference word count. 0.0 = perfect."""

    ref, hyp = _normalize(reference), _normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cost = 0 if r == h else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1] / len(ref)


# --- engines -----------------------------------------------------------------


def transcribe_whisper(pcm: bytes, model_name: str, language: str) -> str:
    """One-shot full-buffer whisper.cpp pass via the production engine."""

    import os

    os.environ.setdefault("STT_MODEL", model_name)
    os.environ.setdefault("STT_LANGUAGE", language)
    from bob.config import Settings
    from bob.stt_engine import WhisperCppSttEngine

    engine = WhisperCppSttEngine(Settings(STT_MODEL=model_name, STT_LANGUAGE=language))
    return engine.transcribe_pcm(pcm)


def _find_model_files(model_dir: Path, *, int8: bool) -> dict[str, str]:
    """Glob a sherpa transducer model dir for the 4 required files.

    Prefers the non-int8 (fp32) weights for a best-case quality read unless
    --int8 is passed; falls back to whatever variant is present.
    """

    def pick(stem: str) -> Path:
        want_int8 = sorted(model_dir.glob(f"{stem}*int8*.onnx"))
        want_fp32 = sorted(p for p in model_dir.glob(f"{stem}*.onnx") if "int8" not in p.name)
        order = (want_int8 + want_fp32) if int8 else (want_fp32 + want_int8)
        if not order:
            raise FileNotFoundError(f"no {stem}*.onnx under {model_dir}")
        return order[0]

    tokens = model_dir / "tokens.txt"
    if not tokens.exists():
        raise FileNotFoundError(f"no tokens.txt under {model_dir}")
    return {
        "encoder": str(pick("encoder")),
        "decoder": str(pick("decoder")),
        "joiner": str(pick("joiner")),
        "tokens": str(tokens),
    }


def build_sherpa(model_dir: Path, *, int8: bool, threads: int):
    import sherpa_onnx

    f = _find_model_files(model_dir, int8=int8)
    print(f"  sherpa encoder: {Path(f['encoder']).name}", file=sys.stderr)
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=f["tokens"],
        encoder=f["encoder"],
        decoder=f["decoder"],
        joiner=f["joiner"],
        num_threads=threads,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        enable_endpoint_detection=False,  # bench whole clip; endpointing is voice_loop's job
    )


def transcribe_sherpa(recognizer, samples: np.ndarray) -> str:
    """Stream the clip through sherpa in CHUNK_SECONDS slices, return final text."""

    stream = recognizer.create_stream()
    step = int(CHUNK_SECONDS * SAMPLE_RATE)
    for start in range(0, len(samples), step):
        stream.accept_waveform(SAMPLE_RATE, samples[start : start + step])
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
    # Tail flush so the last tokens drain (sherpa convention).
    stream.accept_waveform(SAMPLE_RATE, np.zeros(int(0.5 * SAMPLE_RATE), np.float32))
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    return recognizer.get_result(stream).strip()


# --- model download ----------------------------------------------------------


def download_fr_model(dest: Path) -> None:
    import tarfile
    import urllib.request

    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / Path(FR_MODEL_URL).name
    print(f"downloading {FR_MODEL_URL}\n  -> {archive}", file=sys.stderr)
    urllib.request.urlretrieve(FR_MODEL_URL, archive)
    print("extracting...", file=sys.stderr)
    with tarfile.open(archive, "r:bz2") as t:
        t.extractall(dest)
    archive.unlink(missing_ok=True)
    print(f"done. model dir: {dest / archive.stem.removesuffix('.tar')}", file=sys.stderr)


# --- report ------------------------------------------------------------------


@dataclass
class Row:
    clip: str
    duration_s: float
    whisper_text: str
    whisper_rtf: float
    sherpa_text: str
    sherpa_rtf: float
    whisper_wer: float | None
    sherpa_wer: float | None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--download-fr-model", metavar="DIR", type=Path,
                    help="download+extract the 2023 FR zipformer into DIR, then exit")
    ap.add_argument("--sherpa-model-dir", type=Path, help="extracted sherpa transducer model dir")
    ap.add_argument("--wav-dir", type=Path, help="dir of 16k mono s16le .wav clips")
    ap.add_argument("--wav", type=Path, nargs="*", default=[], help="explicit .wav files")
    ap.add_argument("--ref-dir", type=Path,
                    help="dir of <clip>.txt reference transcripts (for WER)")
    ap.add_argument("--whisper-model", default="large-v3-turbo")
    ap.add_argument("--language", default="fr")
    ap.add_argument("--int8", action="store_true", help="prefer int8 sherpa weights (ship-config)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-whisper", action="store_true", help="skip whisper side")
    args = ap.parse_args()

    if args.download_fr_model:
        download_fr_model(args.download_fr_model)
        return 0

    clips: list[Path] = list(args.wav)
    if args.wav_dir:
        clips += sorted(args.wav_dir.glob("*.wav"))
    if not clips:
        ap.error("no clips: pass --wav-dir and/or --wav")
    if not args.sherpa_model_dir:
        ap.error("--sherpa-model-dir required (or --download-fr-model first)")

    recognizer = build_sherpa(args.sherpa_model_dir, int8=args.int8, threads=args.threads)

    rows: list[Row] = []
    for clip in clips:
        pcm = read_wav_pcm(clip)
        samples = pcm_to_float32(pcm)
        duration = len(samples) / SAMPLE_RATE

        whisper_text, whisper_rtf = "", 0.0
        if not args.no_whisper:
            t0 = time.perf_counter()
            whisper_text = transcribe_whisper(pcm, args.whisper_model, args.language)
            whisper_rtf = (time.perf_counter() - t0) / max(duration, 1e-6)

        t0 = time.perf_counter()
        sherpa_text = transcribe_sherpa(recognizer, samples)
        sherpa_rtf = (time.perf_counter() - t0) / max(duration, 1e-6)

        ref = None
        if args.ref_dir:
            ref_path = args.ref_dir / f"{clip.stem}.txt"
            if ref_path.exists():
                ref = ref_path.read_text(encoding="utf-8").strip()

        rows.append(Row(
            clip=clip.name, duration_s=duration,
            whisper_text=whisper_text, whisper_rtf=whisper_rtf,
            sherpa_text=sherpa_text, sherpa_rtf=sherpa_rtf,
            whisper_wer=(
                word_error_rate(ref, whisper_text) if ref and not args.no_whisper else None
            ),
            sherpa_wer=(word_error_rate(ref, sherpa_text) if ref else None),
        ))

    # --- print -------------------------------------------------------------
    for r in rows:
        print(f"\n=== {r.clip}  ({r.duration_s:.1f}s) ===")
        if not args.no_whisper:
            print(f"  whisper (RTF {r.whisper_rtf:.2f}): {r.whisper_text}")
        print(f"  sherpa  (RTF {r.sherpa_rtf:.2f}): {r.sherpa_text}")
        if r.sherpa_wer is not None:
            w = f"{r.whisper_wer:.1%}" if r.whisper_wer is not None else "—"
            print(f"  WER  whisper={w}  sherpa={r.sherpa_wer:.1%}")

    refd = [r for r in rows if r.sherpa_wer is not None]
    if refd:
        s_avg = sum(r.sherpa_wer for r in refd) / len(refd)
        print(f"\n--- mean WER over {len(refd)} labelled clips ---")
        print(f"  sherpa  : {s_avg:.1%}")
        if not args.no_whisper:
            w_avg = sum(r.whisper_wer for r in refd) / len(refd)
            print(f"  whisper : {w_avg:.1%}")
            verdict = "sherpa COMPETITIVE → build SherpaSttEngine" if s_avg <= w_avg + 0.05 \
                else "sherpa WORSE → fall back to faster-whisper/WhisperKit streaming"
            print(f"  gate    : {verdict}")
    else:
        print("\n(no --ref-dir → eyeball FR quality above; supply <clip>.txt for WER gate)")

    print("\nrtf < 1.0 = faster than real time. "
          "sherpa target « true-streaming » → expect ~0.1-0.3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
