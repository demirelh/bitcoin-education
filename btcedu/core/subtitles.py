"""Turn spoken word timings into subtitle cues, SRT files and ASS overlays.

Three things have to come together for a subtitle that sits on the word:

* **When** — ElevenLabs returns a character alignment with every take, so the
  timing comes from the engine that produced the speech rather than from an
  estimate. A take without alignment falls back to spreading its text evenly
  across the measured duration; over a single chapter that is off by a fraction
  of a second, which is a great deal better than no subtitle at all.
* **What** — the viewer must read the narration as written ("1987"), not as
  spoken ("bin dokuz yüz seksen yedi"). The timings arrive on the spoken form,
  so the two are aligned against each other and the written words inherit the
  time of the spoken words they stand for.
* **Where** — a cue is at most two lines of at most 42 characters, and it never
  spans a sentence boundary. Those are the conventions broadcast subtitles have
  settled on; a line that runs wider is read as an error even when it is
  legible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

MAX_LINE_CHARS = 42
MAX_LINES = 2
MIN_CUE_SECONDS = 1.0
MAX_CUE_SECONDS = 6.0
# A cue that disappears the instant the last syllable ends reads as clipped.
_TAIL_SECONDS = 0.25
_SENTENCE_END = re.compile(r"[.!?…]['\"»”’]?$")
_CLAUSE_END = re.compile(r"[,;:]['\"»”’]?$")
_PUNCTUATION = re.compile(r"[^\w]+", re.UNICODE)
# Charged to the word that ends a sentence or a clause, in units of characters,
# to stand for the silence that follows it. Fitted against forced alignment of
# real chapters — see :func:`spread_words`.
_SENTENCE_PAUSE_WEIGHT = 5.0
_CLAUSE_PAUSE_WEIGHT = 2.0


@dataclass
class TimedWord:
    """A written word and the stretch of audio in which it is spoken."""

    word: str
    start: float
    end: float


@dataclass
class Cue:
    """One subtitle, already broken into the lines it will be shown as."""

    start: float
    end: float
    lines: list[str] = field(default_factory=list)
    # A weather card carries its numbers in the lower half of the frame, so a
    # box at the bottom would hide the very data the chapter is about. Those
    # chapters put the line at the top instead.
    at_top: bool = False

    @property
    def text(self) -> str:
        return " ".join(self.lines)


def _match_key(word: str) -> str:
    """A word reduced to what makes it comparable across spellings."""
    return _PUNCTUATION.sub("", word).casefold()


def align_written_to_spoken(written: str, spoken: list[TimedWord]) -> list[TimedWord]:
    """Give each written word the time of the spoken word(s) standing for it.

    Most words are spoken exactly as written and match one to one. The
    interesting cases are the ones the speech normaliser rewrote — "1987"
    becomes four words, "%" becomes one — and there the whole written run
    inherits the whole spoken run, divided by length. That is an approximation
    inside a handful of words, and it is invisible: the cue containing them
    starts and ends at the right moment either way.
    """
    written_words = written.split()
    if not written_words:
        return []
    if not spoken:
        return []

    matcher = SequenceMatcher(
        None,
        [_match_key(item.word) for item in spoken],
        [_match_key(word) for word in written_words],
        autojunk=False,
    )
    result: list[TimedWord] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        targets = written_words[j1:j2]
        if not targets:
            continue
        if tag == "equal":
            result.extend(
                TimedWord(word, spoken[i].start, spoken[i].end)
                for i, word in zip(range(i1, i2), targets, strict=True)
            )
            continue
        # A written run with no spoken counterpart borrows the seam it sits on,
        # so it is never left without a time.
        source = spoken[i1:i2]
        if source:
            start, end = source[0].start, source[-1].end
        else:
            before = spoken[i1 - 1].end if i1 > 0 else 0.0
            after = spoken[i1].start if i1 < len(spoken) else before
            start = end = max(before, after)
        span = max(end - start, 0.0)
        total = sum(len(word) for word in targets) or len(targets)
        cursor = start
        for word in targets:
            share = span * (len(word) / total)
            result.append(TimedWord(word, cursor, cursor + share))
            cursor += share
        if result:
            result[-1] = TimedWord(result[-1].word, result[-1].start, end)
    return result


def spread_words(text: str, duration: float) -> list[TimedWord]:
    """Distribute *text* across *duration* by length, allowing for pauses.

    The fallback for a take whose alignment never arrived. Measured against
    forced alignment of a real 150 s chapter, plain length puts a cue 0.66 s
    off the spoken word at the median; charging a sentence end and a comma for
    the silence that follows them brings that to 0.55 s. What stays is the
    speaker change in the middle of the chapter, where the pause is several
    seconds long and nothing in the text announces it — which is why this is
    the fallback and the engine's own alignment is what gets used when it is
    there.
    """
    words = text.split()
    if not words or duration <= 0:
        return []

    def _weight(word: str) -> float:
        weight = float(len(word))
        if _SENTENCE_END.search(word):
            weight += _SENTENCE_PAUSE_WEIGHT
        elif _CLAUSE_END.search(word):
            weight += _CLAUSE_PAUSE_WEIGHT
        return weight

    weights = [_weight(word) for word in words]
    total = sum(weights)
    result: list[TimedWord] = []
    cursor = 0.0
    for word, weight in zip(words, weights, strict=True):
        share = duration * (weight / total)
        result.append(TimedWord(word, cursor, cursor + share))
        cursor += share
    return result


def _wrap(words: list[str]) -> list[str]:
    """Break a cue into at most two lines of roughly equal length."""
    text = " ".join(words)
    if len(text) <= MAX_LINE_CHARS:
        return [text]
    # Prefer the break that leaves the two lines closest in length; a subtitle
    # with one full line and one word under it reads worse than two even ones.
    best_index = None
    best_score = None
    for index in range(1, len(words)):
        first = " ".join(words[:index])
        second = " ".join(words[index:])
        if len(first) > MAX_LINE_CHARS or len(second) > MAX_LINE_CHARS:
            continue
        score = abs(len(first) - len(second))
        if best_score is None or score < best_score:
            best_index, best_score = index, score
    if best_index is None:
        # Does not fit in two lines. Rather than drop words, run the second
        # line long — an over-wide line is readable, a missing sentence is not.
        half = len(words) // 2 or 1
        return [" ".join(words[:half]), " ".join(words[half:])]
    return [" ".join(words[:best_index]), " ".join(words[best_index:])]


def build_cues(words: list[TimedWord], *, at_top: bool = False) -> list[Cue]:
    """Group timed words into readable cues.

    Sentences are never mixed: a cue that ends mid-sentence and resumes under
    the next picture is the single most common way subtitles become hard to
    follow. Within a sentence the split goes at a comma when there is one.
    """
    cues: list[Cue] = []
    pending: list[TimedWord] = []

    def _flush() -> None:
        if not pending:
            return
        cues.append(
            Cue(
                start=pending[0].start,
                end=pending[-1].end,
                lines=_wrap([item.word for item in pending]),
                at_top=at_top,
            )
        )
        pending.clear()

    for word in words:
        candidate = pending + [word]
        too_long = len(" ".join(item.word for item in candidate)) > MAX_LINE_CHARS * MAX_LINES
        too_slow = candidate[-1].end - candidate[0].start > MAX_CUE_SECONDS
        if pending and (too_long or too_slow):
            # The clause boundary is the better place to have stopped, so back
            # up to it rather than cutting mid-phrase.
            split = None
            for index in range(len(pending) - 1, 0, -1):
                if _CLAUSE_END.search(pending[index].word):
                    split = index + 1
                    break
            if split is not None and split < len(pending):
                tail = pending[split:]
                del pending[split:]
                _flush()
                pending.extend(tail)
            else:
                _flush()
        pending.append(word)
        if _SENTENCE_END.search(word.word):
            _flush()
    _flush()

    for index, cue in enumerate(cues):
        wanted = max(cue.end + _TAIL_SECONDS, cue.start + MIN_CUE_SECONDS)
        if index + 1 < len(cues):
            # Never run into the next cue — two subtitles on screen at once is
            # worse than one that leaves a touch early.
            cue.end = min(wanted, max(cues[index + 1].start, cue.end))
        else:
            cue.end = wanted
    return cues


def chapter_cues(
    narration: str,
    word_timings: list[dict] | None,
    duration: float,
    *,
    at_top: bool = False,
) -> list[Cue]:
    """Cues for one chapter, timed relative to the start of its audio."""
    spoken = [
        TimedWord(str(item["w"]), float(item["s"]), float(item["e"]))
        for item in (word_timings or [])
        if isinstance(item, dict) and {"w", "s", "e"} <= set(item)
    ]
    if spoken:
        words = align_written_to_spoken(narration, spoken)
    else:
        words = spread_words(narration, duration)
    return build_cues(words, at_top=at_top)


def shift(cues: list[Cue], offset: float) -> list[Cue]:
    return [
        Cue(cue.start + offset, cue.end + offset, list(cue.lines), cue.at_top) for cue in cues
    ]


def window(cues: list[Cue], start: float, end: float) -> list[Cue]:
    """The cues visible between *start* and *end*, clipped to that window.

    Used when a chapter is rendered as several shots: each shot burns in only
    what is spoken while it is on screen, and a cue straddling the cut is
    trimmed rather than shown twice in full.
    """
    result: list[Cue] = []
    for cue in cues:
        if cue.end <= start or cue.start >= end:
            continue
        result.append(
            Cue(
                max(cue.start, start) - start,
                min(cue.end, end) - start,
                list(cue.lines),
                cue.at_top,
            )
        )
    return result


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:  # pragma: no cover - rounding edge
        millis, secs = 0, secs + 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(cues: list[Cue]) -> str:
    """SRT, which is what YouTube accepts for a caption track."""
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{_srt_timestamp(cue.start)} --> {_srt_timestamp(cue.end)}\n"
            + "\n".join(cue.lines)
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    centis = int((seconds - int(seconds)) * 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


@dataclass
class SubtitleStyle:
    """How burned-in subtitles look. Profile-owned, with broadcast defaults."""

    font: str = "Roboto Condensed"
    font_size: int = 52
    primary_colour: str = "&H00FFFFFF"
    outline_colour: str = "&H00000000"
    back_colour: str = "&H80000000"
    outline: float = 2.5
    shadow: float = 0.0
    # 3 draws an opaque box behind the line, 1 draws an outline only. A box is
    # what news subtitles use, and on a photograph it is the difference between
    # legible and nearly legible.
    border_style: int = 3
    # Clear of the ticker band at the bottom of the frame.
    margin_v: int = 130
    bold: int = 0
    play_res_x: int = 1920
    play_res_y: int = 1080


def to_ass(cues: list[Cue], style: SubtitleStyle | None = None) -> str:
    """ASS, which is what libass burns into the picture.

    SRT would also burn, but only ASS carries the styling with it; passing the
    look through ``force_style`` on the command line means every renderer has
    to be told about it separately.
    """
    style = style or SubtitleStyle()
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {style.play_res_x}\n"
        f"PlayResY: {style.play_res_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style.font},{style.font_size},{style.primary_colour},"
        f"{style.primary_colour},{style.outline_colour},{style.back_colour},{style.bold},"
        f"0,0,0,100,100,0,0,{style.border_style},{style.outline},{style.shadow},2,"
        f"60,60,{style.margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [
        f"Dialogue: 0,{_ass_timestamp(cue.start)},{_ass_timestamp(cue.end)},Default,,0,0,0,,"
        + ("{\\an8}" if cue.at_top else "")
        + "\\N".join(line.replace("\n", " ") for line in cue.lines)
        for cue in cues
    ]
    return header + "\n".join(lines) + ("\n" if lines else "")
