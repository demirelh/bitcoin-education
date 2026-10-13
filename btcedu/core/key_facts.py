"""On-screen key facts: verbatim spans of the narration, shown as they are spoken.

A key fact is never written for the screen. It is a contiguous piece of the
approved narration, so the card can only repeat what the viewer hears — a span
that cannot be found word for word is dropped rather than shown.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

logger = logging.getLogger(__name__)

MAX_ITEMS = 4
MAX_ITEM_CHARS = 60
DEFAULT_TITLE = "ÖNE ÇIKANLAR"
RUNDOWN_TITLE = "BUGÜN"
# How long the card stays after its last item appeared.
CARD_HOLD_SECONDS = 10.0
_END_MARGIN_SECONDS = 0.3
_PUNCTUATION = re.compile(r"[^\w]+", re.UNICODE)


def _fold(word: str) -> str:
    # Dotted and dotless i fold together: capitals are written either way.
    return _PUNCTUATION.sub("", word).replace("İ", "i").lower().replace("ı", "i")


def _clean(item: object) -> str:
    return " ".join(str(item or "").split()).strip(" .,;:")


def find_span(narration: str, item: str) -> int | None:
    """Index in ``narration.split()`` where *item* starts, or None."""
    indexed = [(i, k) for i, k in enumerate(_fold(w) for w in narration.split()) if k]
    target = [k for k in (_fold(w) for w in item.split()) if k]
    if not target:
        return None
    keys = [k for _, k in indexed]
    for pos in range(len(keys) - len(target) + 1):
        if keys[pos : pos + len(target)] == target:
            return indexed[pos][0]
    return None


def verified_items(narration: str, items: list | None) -> list[str]:
    """The items that occur verbatim in *narration*, in spoken order."""
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in items or []:
        item = _clean(raw)
        if not item or len(item) > MAX_ITEM_CHARS:
            continue
        key = " ".join(_fold(w) for w in item.split())
        if key in seen:
            continue
        position = find_span(narration, item)
        if position is None:
            logger.info("Key fact dropped, not verbatim in the narration: %r", item)
            continue
        seen.add(key)
        found.append((position, item))
    found.sort(key=lambda pair: pair[0])
    return [item for _, item in found[:MAX_ITEMS]]


def _timed_words(narration: str, word_timings: list[dict] | None, duration: float) -> list:
    from btcedu.core.subtitles import TimedWord, align_written_to_spoken, spread_words

    spoken = [
        TimedWord(str(item["w"]), float(item["s"]), float(item["e"]))
        for item in (word_timings or [])
        if isinstance(item, dict) and {"w", "s", "e"} <= set(item)
    ]
    if spoken:
        return align_written_to_spoken(narration, spoken)
    return spread_words(narration, duration)


def item_times(
    narration: str, items: list[str], word_timings: list[dict] | None, duration: float
) -> list[float | None]:
    """When each item starts being spoken, relative to the chapter audio."""
    words = _timed_words(narration, word_timings, duration)
    times: list[float | None] = []
    for item in items:
        position = find_span(narration, item)
        if position is None or position >= len(words):
            times.append(None)
        else:
            times.append(round(words[position].start, 2))
    return times


def rundown(opening: str, stories) -> tuple[list[str], list[str]]:
    """Headlines of the stories the opening teases, with the teaser that times each.

    The opening reads one teaser per top story, taken from its summary or its
    first anchor sentence. A story counts as teased only if that text is found
    word for word in the opening narration.
    """
    items: list[tuple[int, str, str]] = []
    for story in stories:
        headline = (getattr(story, "display_headline", "") or "").strip()
        if not headline:
            continue
        candidates = [_clean(getattr(story, "display_summary", ""))]
        candidates += [
            " ".join(segment.text.split()[:8])
            for segment in getattr(story, "speaker_sequence", [])[:1]
            if segment.text.strip()
        ]
        for anchor in candidates:
            position = find_span(opening, anchor) if len(anchor.split()) >= 3 else None
            if position is not None:
                items.append((position, headline, anchor))
                break
    items.sort(key=lambda entry: entry[0])
    return [headline for _, headline, _ in items], [anchor for _, _, anchor in items]


def display_text(item: str) -> str:
    first = item[:1]
    head = "İ" if first == "i" else first.upper()
    return head + item[1:]


def card_window(times: list[float], duration: float) -> tuple[float, float] | None:
    """Start and end of the card, or None if nothing fits into the chapter."""
    if not times:
        return None
    start = times[0]
    end = min(duration - _END_MARGIN_SECONDS, times[-1] + CARD_HOLD_SECONDS)
    if end - start < 1.0:
        return None
    return start, end


def shot_overlays(overlays: list, index: int, start: float, end: float) -> list:
    """The overlays one shot of a multi-shot chapter draws, timed to that shot.

    The lower third belongs to the story and stays on the first shot. A key
    facts card follows the narration across cuts; an item spoken before the cut
    keeps its (negative) time and is therefore already fully visible.
    """
    result = []
    for spec in overlays or []:
        if getattr(spec, "overlay_type", "") != "key_facts":
            if index == 0:
                result.append(spec)
            continue
        if spec.end <= start or spec.start >= end:
            continue
        items = [(text, round(at - start, 3)) for text, at in spec.items if at < end]
        if not items:
            continue
        result.append(
            replace(
                spec,
                start=round(spec.start - start, 3),
                end=round(min(spec.end, end) - start, 3),
                items=items,
            )
        )
    return result
