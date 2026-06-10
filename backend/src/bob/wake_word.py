"""Wake-word detection — « Yo Bob » arms the listening turn.

When ``WAKE_WORD_ENABLED`` is on, the full-duplex loop starts each armed
window in **standby**: the mic streams frames but NO turn opens and the main
STT engine stays cold (zero continuous GPU). A small whisper model (default
``tiny``) transcribes a short rolling window of recent audio — VAD-gated and
debounced — and a fuzzy matcher checks the hypothesis for the wake phrase.
On a match the loop opens a real turn (the orb flips to « écoute »), seeds
the main STT with the rolling buffer (so a same-breath command — « Yo Bob,
quelle heure est-il ? » — is fully captured), and the wake phrase is stripped
from the frozen transcript before it reaches the say-path.

Why fuzzy matching: the small model mishears the phrase routinely (« Yo Bob »
→ « Yobab » / « yo bob ! » / « Yo, Bob »), so an exact substring test would
miss most real utterances. The matcher normalizes (lowercase, accents folded,
punctuation dropped, spaces collapsed *out*) and compares sliding word
windows against the phrase with :class:`difflib.SequenceMatcher`; the
threshold trades false accepts against false rejects.

The detector is engine-agnostic: it takes a ``transcriber`` callable
(``bytes`` s16le PCM → text) so tests drive it with a scripted function and
production wires a dedicated :class:`bob.stt_engine.WhisperCppSttEngine`
loaded with the small model.
"""

from __future__ import annotations

import asyncio
import unicodedata
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

_logger = structlog.get_logger(__name__)

_BYTES_PER_SAMPLE = 2


def normalise_text(text: str) -> str:
    """Lowercase, fold accents, drop punctuation, collapse whitespace.

    The canonical form both the live hypothesis and the configured phrase are
    reduced to before comparison, so « Yo, Bob ! » and ``yo bob`` meet in the
    middle.
    """

    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in stripped)
    return " ".join(cleaned.split())


def _squash(text: str) -> str:
    """Remove spaces from a normalised string (compare « yobab » to « yo bob »)."""

    return text.replace(" ", "")


@dataclass(frozen=True)
class WakeMatch:
    """A confirmed wake-phrase hit in a transcription hypothesis."""

    #: The raw hypothesis the small model produced (debug / event payload).
    text: str
    #: The matcher's similarity score in [0, 1] for the winning window.
    score: float


def match_wake_phrase(text: str, phrase: str, *, threshold: float = 0.75) -> WakeMatch | None:
    """Fuzzy-search ``phrase`` in ``text``; return the hit or ``None``.

    Slides word windows of ±1 the phrase's word count over the normalised
    hypothesis and scores each window (spaces squashed on both sides) with
    :class:`difflib.SequenceMatcher`. The best window at/above ``threshold``
    wins. Pure and cheap — hypotheses are a handful of words.
    """

    norm_phrase = _squash(normalise_text(phrase))
    if not norm_phrase:
        return None
    words = normalise_text(text).split()
    if not words:
        return None
    phrase_words = max(1, len(normalise_text(phrase).split()))
    best = 0.0
    for size in range(max(1, phrase_words - 1), phrase_words + 2):
        for start in range(0, max(0, len(words) - size) + 1):
            candidate = _squash("".join(words[start : start + size]))
            if not candidate:
                continue
            score = SequenceMatcher(None, candidate, norm_phrase).ratio()
            if score > best:
                best = score
    if best >= threshold:
        return WakeMatch(text=text, score=round(best, 3))
    return None


def strip_wake_prefix(text: str, phrase: str, *, threshold: float = 0.75) -> str:
    """Cut the leading wake phrase out of a frozen transcript.

    Finds the first fuzzy occurrence of ``phrase`` within the transcript's
    first few words and returns everything after it (leading punctuation and
    whitespace trimmed). A transcript without a confident match is returned
    unchanged; a transcript that IS just the wake phrase returns ``""`` (the
    caller acknowledges instead of running the say-path on an empty command).
    """

    norm_phrase = _squash(normalise_text(phrase))
    if not norm_phrase or not text.strip():
        return text
    phrase_words = max(1, len(normalise_text(phrase).split()))

    # Tokenize the ORIGINAL text with character spans so the cut lands on the
    # raw string, not the normalised one.
    spans: list[tuple[int, int, str]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isalnum():
            j = i
            while j < n and text[j].isalnum():
                j += 1
            spans.append((i, j, normalise_text(text[i:j])))
            i = j
        else:
            i += 1

    # The wake phrase is a PREFIX phenomenon: only consider windows starting
    # within the first few tokens so a late « yo bob » inside the command is
    # never cut out of the middle.
    max_start = min(len(spans), 2)
    for start in range(max_start):
        for size in range(max(1, phrase_words - 1), phrase_words + 2):
            end = start + size
            if end > len(spans):
                continue
            candidate = _squash("".join(tok for _, _, tok in spans[start:end]))
            if not candidate:
                continue
            score = SequenceMatcher(None, candidate, norm_phrase).ratio()
            if score >= threshold:
                cut = spans[end - 1][1]
                rest = text[cut:].lstrip(" \t\n,.;:!?…-—")
                return rest
    return text


class WakeWordDetector:
    """Rolling-window wake-phrase detector over decoded mic PCM.

    Feed every standby frame via :meth:`feed`; it keeps a bounded ring of the
    most recent ``window_samples`` of audio, and once at least
    ``min_speech_frames`` speech frames landed since the last reset AND
    ``interval_samples`` of new audio accumulated since the previous pass, it
    runs ``transcriber`` (in a thread — whisper is blocking) on the ring and
    fuzzy-matches the hypothesis. Silence resets the speech gate so ambient
    noise never pays an inference.

    Single-flight: one transcription at a time; frames arriving mid-pass keep
    accumulating and the next due frame triggers the next pass.
    """

    def __init__(
        self,
        *,
        transcriber: Callable[[bytes], str],
        phrase: str,
        sample_rate: int = 16_000,
        window_seconds: float = 2.5,
        interval_seconds: float = 0.7,
        threshold: float = 0.75,
        min_speech_frames: int = 3,
    ) -> None:
        self._transcriber = transcriber
        self._phrase = phrase
        self._threshold = threshold
        self._window_bytes = max(1, round(window_seconds * sample_rate)) * _BYTES_PER_SAMPLE
        self._interval_samples = max(1, round(interval_seconds * sample_rate))
        self._min_speech_frames = max(1, min_speech_frames)
        self._ring: deque[bytes] = deque()
        self._ring_bytes = 0
        self._samples_since_pass = 0
        self._speech_frames = 0
        self._busy = False

    @property
    def phrase(self) -> str:
        """The configured wake phrase (for the strip step and events)."""

        return self._phrase

    def recent_audio(self) -> bytes:
        """The rolling window's PCM — seeds the main STT turn on a wake hit."""

        return b"".join(self._ring)

    def reset(self) -> None:
        """Drop the ring + gates (called after a wake hit / on teardown)."""

        self._ring.clear()
        self._ring_bytes = 0
        self._samples_since_pass = 0
        self._speech_frames = 0

    async def feed(self, pcm: bytes, *, is_speech: bool) -> WakeMatch | None:
        """Feed one decoded standby frame; return a match when the phrase hit.

        The transcription runs in a worker thread (the same offload pattern as
        :meth:`bob.voice_turn.VoiceTurn.feed_frame`); a pass that errors is
        logged and swallowed — a wake hiccup must never take the session down.
        """

        self._ring.append(pcm)
        self._ring_bytes += len(pcm)
        while self._ring_bytes > self._window_bytes and len(self._ring) > 1:
            dropped = self._ring.popleft()
            self._ring_bytes -= len(dropped)

        if is_speech:
            self._speech_frames += 1
        self._samples_since_pass += len(pcm) // _BYTES_PER_SAMPLE

        if (
            self._busy
            or self._speech_frames < self._min_speech_frames
            or self._samples_since_pass < self._interval_samples
        ):
            return None

        self._samples_since_pass = 0
        self._busy = True
        try:
            text = await asyncio.to_thread(self._transcriber, self.recent_audio())
        except Exception:
            _logger.exception("wake_word.pass_failed")
            return None
        finally:
            self._busy = False
        if not text:
            return None
        match = match_wake_phrase(text, self._phrase, threshold=self._threshold)
        if match is not None:
            _logger.info("wake_word.detected", text=text, score=match.score)
        return match
