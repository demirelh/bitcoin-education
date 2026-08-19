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

Short takes are examined in full, long ones only at the opening
------------------------------------------------------------
Looking at four seconds was a compromise made for the Pi's sake, and on
2026-08-18 it cost an episode: the voice read "gününün öne çıkan" at second
five, past the window, and the check saw nothing. Every framing line — the
greeting, the headline hand-off, the weather hand-off, the sign-off — is under
half a minute, and those are exactly the lines that repeat in every bulletin
and sit where a listener cannot miss them. Transcribing all of one costs a few
seconds of CPU. A chapter body runs to four minutes and is transcribed at the
opening only, as before.

A full transcript also allows a second, blunter question that a four-second
window cannot support: did the take read roughly the text it was given? Not
word for word — the recogniser is not accurate enough for that — but closely
enough that a take which trails off into syllable salad, or swallows its first
sentence, is told apart from one the recogniser merely misspelled.
"""

from __future__ import annotations

import difflib
import logging
import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Seconds of audio to examine when a take is too long to check in full. Both
#: originally observed stumbles were over within the first two seconds; four
#: gives the recogniser enough context to settle.
_HEAD_SECONDS = 4.0

#: Takes up to this long are transcribed end to end. This covers every framing
#: line the profile holds (the longest is around fifteen seconds) and no
#: chapter body, which is the intent: on a Pi `small` transcribes roughly in
#: real time, so half a minute of audio is an acceptable price per take and
#: four minutes is not.
_FULL_CHECK_MAX_SECONDS = 30.0

#: How closely a full take has to match its script. Deliberately loose. Over 32
#: stored takes the recogniser misspelled a word in a third of them, which on a
#: fifteen-word line costs a few points; a take that ran off the script scores
#: far below this. The threshold has to separate those two, nothing finer.
_MIN_SIMILARITY = 0.72

#: Below this many words a similarity score says little — one misheard word out
#: of four is already a third of the text.
_MIN_WORDS_FOR_SIMILARITY = 8

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

#: A syllable of two or three letters immediately repeated inside one word —
#: the "ünün" in "gününün". Two letters is the shortest unit a Turkish voice
#: restarts on; more than three stops being a syllable.
_DOUBLED_SYLLABLE = re.compile(r"(\w{2,3})\1", re.UNICODE)


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


def _max_in_window(words: list[str], word: str, size: int) -> int:
    """The most times *word* appears in any *size* consecutive *words*.

    A script may well say the same word twice, pages apart; what excuses a
    take is the script saying it twice as closely as the take did.
    """
    best = 0
    for i in range(max(1, len(words))):
        count = words[i : i + size].count(word)
        if count > best:
            best = count
    return best


def find_repetition(expected: str, heard: str, *, whole: bool = False) -> str:
    """The word the take repeats but the script does not, if there is one.

    The script is consulted only to rule out repetitions that were asked for.
    A headline block may legitimately open "Bugün, bugün..." and rejecting that
    would cost a paid retry for a take that is exactly right.

    Two shapes are looked for, because the stumble takes two forms. It can
    repeat a whole word — "İyi Dağ'ım, İyi Akşamlar" — or fold the repetition
    into neighbouring words, as in "Önce gününün akgününe çıkan" for a script
    reading "Önce günün öne çıkan". The second is invisible to a word-level
    comparison and needs the stem looked for inside words as well.

    With *whole* the same window slides across the entire transcript instead of
    resting on the opening, and is compared against the entire script. Sliding
    is what makes it find a stumble at second five; comparing against the whole
    script is what stops it inventing one, since a phrase that recurs three
    chapters apart is not a stumble.
    """
    heard_words = _normalise(heard)
    if not heard_words:
        return ""
    expected_words = _normalise(expected)
    if not whole:
        heard_words = heard_words[:_WINDOW_WORDS]
        expected_words = expected_words[: _WINDOW_WORDS * 2]

    if whole:
        # Across a whole take the test is deliberately stricter than at the
        # opening: the same word twice within three, not merely the same
        # five-letter stem within six. Stems conflate distinct words — "yerine"
        # and "yerinde" share one — and over twenty words that collision is
        # near certain, which is how a correct take came to be rejected for
        # "repeating 'yerin'" on 2026-08-19. A voice that stumbles says the
        # same word again, and says it right away.
        for i in range(len(heard_words)):
            window = heard_words[i : i + 3]
            for word, count in Counter(w for w in window if len(w) >= _MIN_WORD_LENGTH).items():
                if count > 1 and _max_in_window(expected_words, word, 3) < count:
                    return word
        return ""

    expected_counts = Counter(_stems(expected_words))
    for stem, count in Counter(_stems(heard_words)).items():
        if count > 1 and expected_counts[stem] < count:
            return stem

    # The same repetition, hidden inside words the recogniser ran together.
    # Only long stems take part: a short one ("ren", "bir") turns up inside
    # unrelated words often enough to reject good takes.
    #
    # Confined to the opening on purpose. Counting a stem across a whole take
    # rejected a correct one on 2026-08-19 ("repeated 'yerin'" on a line that
    # read fine): over twenty words there is always some stem the recogniser
    # produced once more than the script did, and the longer the text the more
    # certain that is. Within six words it is evidence; across a paragraph it
    # is noise. A stumble later in a take is caught by the doubled-syllable and
    # similarity checks instead.
    if whole:
        return ""
    heard_joined = " ".join(heard_words)
    expected_joined = " ".join(expected_words)
    for word in expected_words:
        stem = word[:_STEM_LENGTH]
        if len(stem) < _STEM_LENGTH:
            continue
        if heard_joined.count(stem) > expected_joined.count(stem):
            return stem
    return ""


def similarity(expected: str, heard: str) -> float:
    """How much of the script the take actually read, between 0 and 1.

    Compared on stems rather than whole words so the recogniser's habitual
    inflection errors ("uluslararası" as "uluslar arası") cost little, and
    order-insensitively enough that a dropped comma does not.
    """
    expected_stems = _stems(_normalise(expected))
    heard_stems = _stems(_normalise(heard))
    if not expected_stems:
        return 1.0
    return difflib.SequenceMatcher(None, expected_stems, heard_stems).ratio()


def find_doubled_syllable(expected: str, heard: str) -> str:
    """A word the take stutters *inside*, like "gününün" for "günün".

    The repetition checks above look between words. This one looks within: the
    voice restarts a syllable mid-word and produces something the script never
    contained. It is the shape complained about on 2026-08-18 — the greeting
    read "gününün öne çıkan" — and no comparison at word level can see it,
    because "gününün" is simply a different word.

    A word only counts as evidence when it does **not** appear in the script.
    Turkish has legitimate doubled syllables, and any that were asked for are
    in the text; what is left is the voice's own invention.
    """
    expected_words = set(_normalise(expected))
    for word in _normalise(heard):
        if len(word) < _MIN_WORD_LENGTH + 2 or word in expected_words:
            continue
        if not _DOUBLED_SYLLABLE.search(word):
            continue
        # Only when the script has a word this one grew out of. Without that
        # anchor an unrelated misrecognition would be read as a stumble.
        stem = word[:_STEM_LENGTH]
        if any(other.startswith(stem[:_MIN_WORD_LENGTH]) for other in expected_words):
            return word
    return ""


def swallowed_opening(expected: str, heard: str) -> bool:
    """Did the take start somewhere other than at the beginning of its script?

    Observed as "24 başlıyor" for a line that opens "İyi akşamlar, Almanya 24
    başlıyor" — two words missing out of twenty, which barely moves a
    similarity score but is the first thing a listener hears.

    Judged generously: the take is only faulted when *neither* of the script's
    first two content words turns up anywhere near the start. One misheard
    opening word therefore costs nothing, which matters because a false
    rejection here is a paid retry.
    """
    expected_stems = _stems(_normalise(expected))[:2]
    if not expected_stems:
        return False
    heard_stems = _stems(_normalise(heard))[:4]
    if not heard_stems:
        return False
    return not any(stem in heard_stems for stem in expected_stems)


def _transcribe(audio: Path, *, model, language: str, seconds: float | None) -> str | None:
    """*audio* as text — all of it, or its first *seconds* — or ``None``."""
    with tempfile.TemporaryDirectory(prefix="btcedu-stutter-") as tmp:
        head = Path(tmp) / "head.wav"
        command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(audio)]
        if seconds is not None:
            command += ["-t", str(seconds)]
        command += ["-ar", "16000", "-ac", "1", str(head)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not head.exists():
            logger.debug("could not extract the audio of %s", audio.name)
            return None
        segments, _info = model.transcribe(str(head), language=language)
        return " ".join(segment.text for segment in segments).strip()


def _duration_seconds(audio: Path) -> float | None:
    """How long *audio* runs, or ``None`` when that cannot be established."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio),
            ],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def check_take(audio: Path, expected_text: str, *, model, language: str = "tr") -> StutterVerdict:
    """Did *audio* read its script, or did the voice stumble?

    A take short enough to transcribe in full is judged on all of it: on any
    repetition anywhere, on a syllable doubled inside a word, and on whether
    what was heard resembles the script at all. A long one is judged on its
    opening only, as the four-second check always was.

    Never raises. This is a quality check on a take that already exists; a
    failure to run it must cost the verdict, not the episode.
    """
    if model is None or not expected_text.strip():
        return StutterVerdict(None, reason="unavailable")

    duration = _duration_seconds(audio)
    whole = duration is not None and duration <= _FULL_CHECK_MAX_SECONDS
    try:
        heard = _transcribe(
            audio,
            model=model,
            language=language,
            seconds=None if whole else _HEAD_SECONDS,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("stutter check failed (%s: %s)", type(exc).__name__, exc)
        return StutterVerdict(None, reason="unavailable")

    if not heard:
        return StutterVerdict(None, reason="unavailable")

    repeated = find_repetition(expected_text, heard, whole=whole)
    if repeated:
        return StutterVerdict(True, reason=f"repeated {repeated!r}", heard=heard)

    if not whole:
        return StutterVerdict(False, heard=heard)

    doubled = find_doubled_syllable(expected_text, heard)
    if doubled:
        return StutterVerdict(True, reason=f"stumbled inside {doubled!r}", heard=heard)

    if swallowed_opening(expected_text, heard):
        return StutterVerdict(True, reason="the line does not start where it should", heard=heard)

    # Last and loosest: did it read the line at all? This is what catches a
    # take that trails off into syllables or swallows its first sentence,
    # neither of which repeats anything.
    if len(_normalise(expected_text)) >= _MIN_WORDS_FOR_SIMILARITY:
        score = similarity(expected_text, heard)
        if score < _MIN_SIMILARITY:
            return StutterVerdict(
                True, reason=f"only {score:.0%} of the line was read", heard=heard
            )

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
