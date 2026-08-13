"""Catching takes where the voice stumbles over the opening words.

Generation is stochastic in more ways than the noise check knows about. On
2026-08-13 the greeting came back as "İyi Dağ'ım, İyi Akşamlar, Almanya 24'e
hoş geldiniz" — the opening was spoken twice with a fabricated word wedged in
between. The audio was flawless by every measure taken at the time: the level
was right, the duration was plausible, the noise floor was -62 dB. Only the
words were wrong, and nothing looked at the words.

That failure is expensive out of proportion to how often it happens. It sits in
the first second of the bulletin, where it is impossible to miss, and it is a
recurring line — so the cache stores it and every future episode opens with the
same stumble until someone notices by ear.

Why not simply compare the transcript to the text
-------------------------------------------------
Because the transcript is not good enough to be compared. Measured over 32
stored takes, a `tiny` model misread a third of them in ways that have nothing
to do with the audio: "mahkemesi" as "makemesi", "uluslararası" as "uluslar
arası", "Ren nehrindeki" as "Rennehrindeki". A word-by-word check would reject
takes that are perfectly fine, and every rejection is a paid retry.

So this does not check *whether the words are right*. It checks for the one
thing a misreading cannot produce: the take saying something **twice** that the
script says once. A recogniser that mishears a word substitutes another word;
it does not invent a repetition. That makes the signal robust even when the
transcript around it is mangled — in the case above, `tiny` rendered the
stumble as "iyi dam iyi akşamlar", which is unusable as text but unmistakable
as a repetition.

Only the opening is examined. Both observed stumbles were in the first words,
and transcribing four seconds rather than a full take is what keeps the check
affordable on a Pi.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Seconds of audio to examine. Both observed stumbles were over within the
#: first two seconds; four gives the recogniser enough context to settle.
_HEAD_SECONDS = 4.0

#: How many words of the opening take part. Long enough to contain a repeated
#: phrase, short enough that a legitimate repetition later in the sentence does
#: not reach it.
_WINDOW_WORDS = 6

#: Words shorter than this are ignored. Turkish is full of short function words
#: ("bu", "ve", "de") that repeat legitimately and carry no evidence.
_MIN_WORD_LENGTH = 3

#: Compared on a prefix rather than in full, because the recogniser inflects
#: and joins freely: the stumble that produced "gününün ... akgününe" for a
#: script saying "günün" once is only visible if "günün" matches "gününün".
_STEM_LENGTH = 5

_WORD_SPLIT = re.compile(r"[^\wçğıöşüÇĞİÖŞÜ]+", re.UNICODE)


@dataclass(frozen=True)
class StutterVerdict:
    """Whether a take stumbles over its opening.

    ``stuttered`` is three-valued in effect: ``False`` with an empty ``reason``
    means checked and clean, while ``None`` means no check was possible. They
    are kept apart so a missing model can never be read as a passing grade.
    """

    stuttered: bool | None
    reason: str = ""
    heard: str = ""


def _normalise(text: str) -> list[str]:
    lowered = text.lower().replace("i̇", "i").replace("İ", "i")
    return [w for w in _WORD_SPLIT.split(lowered) if w]


def _stems(words: list[str]) -> list[str]:
    return [w[:_STEM_LENGTH] for w in words if len(w) >= _MIN_WORD_LENGTH]


def find_repetition(expected: str, heard: str) -> str:
    """The word the take repeats but the script does not, if there is one.

    The script is consulted only to rule out repetitions that were asked for.
    A headline block may legitimately open "Bugün, bugün..." and rejecting that
    would cost a paid retry for a take that is exactly right.

    Two shapes are looked for, because the stumble takes two forms. It can
    repeat a whole word — "İyi Dağ'ım, İyi Akşamlar" — or fold the repetition
    into neighbouring words, as in "Önce gününün akgününe çıkan" for a script
    reading "Önce günün öne çıkan". The second is invisible to a word-level
    comparison and needs the stem looked for inside words as well.
    """
    heard_words = _normalise(heard)[:_WINDOW_WORDS]
    if not heard_words:
        return ""
    expected_words = _normalise(expected)[: _WINDOW_WORDS * 2]

    expected_counts = Counter(_stems(expected_words))
    for stem, count in Counter(_stems(heard_words)).items():
        if count > 1 and expected_counts[stem] < count:
            return stem

    # The same repetition, hidden inside words the recogniser ran together.
    # Only long stems take part: a short one ("ren", "bir") turns up inside
    # unrelated words often enough to reject good takes.
    heard_joined = " ".join(heard_words)
    expected_joined = " ".join(expected_words)
    for word in expected_words:
        stem = word[:_STEM_LENGTH]
        if len(stem) < _STEM_LENGTH:
            continue
        if heard_joined.count(stem) > expected_joined.count(stem):
            return stem
    return ""


def _transcribe_opening(audio: Path, *, model, language: str) -> str | None:
    """The first seconds of *audio* as text, or ``None`` if that is not possible."""
    with tempfile.TemporaryDirectory(prefix="btcedu-stutter-") as tmp:
        head = Path(tmp) / "head.wav"
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-i", str(audio),
                "-t", str(_HEAD_SECONDS),
                "-ar", "16000", "-ac", "1",
                str(head),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not head.exists():
            logger.debug("could not extract the opening of %s", audio.name)
            return None
        segments, _info = model.transcribe(str(head), language=language)
        return " ".join(segment.text for segment in segments).strip()


def check_opening(
    audio: Path, expected_text: str, *, model, language: str = "tr"
) -> StutterVerdict:
    """Does *audio* open by saying something twice that the script says once?

    Never raises. This is a quality check on a take that already exists; a
    failure to run it must cost the verdict, not the episode.
    """
    if model is None or not expected_text.strip():
        return StutterVerdict(None, reason="unavailable")
    try:
        heard = _transcribe_opening(audio, model=model, language=language)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("stutter check failed (%s: %s)", type(exc).__name__, exc)
        return StutterVerdict(None, reason="unavailable")

    if not heard:
        return StutterVerdict(None, reason="unavailable")

    repeated = find_repetition(expected_text, heard)
    if repeated:
        return StutterVerdict(True, reason=f"repeated {repeated!r}", heard=heard)
    return StutterVerdict(False, heard=heard)


def load_model(name: str, *, cache_dir: str = ""):
    """The recogniser used for the check, or ``None`` if it is not available.

    Kept separate so callers can load it once per episode rather than once per
    take: on a Pi the model costs more to load than a check costs to run.
    """
    if not name:
        return None
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.info("faster-whisper is not installed; takes will not be checked for stutters")
        return None
    try:
        return WhisperModel(
            name, device="cpu", compute_type="int8", download_root=cache_dir or None
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not load the stutter-check model %r: %s", name, exc)
        return None
