"""Deterministic narration-fidelity lock shared by QA and chapterization.

Phase 8 requirement A: chapterization may create chapter *boundaries*, visuals,
overlays, image prompts and metadata (titles/notes), but it must not rewrite,
add, remove or reorder the spoken narration content — the facts, numbers, names
and results. The approved narration is fixed upstream (translation + adaptation,
gated by QA). This module provides the deterministic comparison used to prove a
chapterized document is a faithful *partition* of that approved narration.

The comparison is intentionally strict: after a *narrowly defined* set of
technical normalizations, the concatenation of chapter narration (in chapter
order) must equal the approved narration character-for-character.

Allowed technical normalizations (and ONLY these):
  * Unicode NFKC canonicalization
  * typographic quotes  → straight ASCII quotes
  * typographic dashes  → ASCII hyphen-minus
  * whitespace / newline runs collapsed to a single space
  * whitespace removed *before* punctuation (punctuation spacing)

Explicitly NOT normalized: digits, letters/words, names, casing or ordering.
A changed digit, a dropped name, an added sentence or a reordered paragraph all
produce a mismatch and fail the lock closed.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

# Typographic → ASCII quote/dash folding. These are pure glyph substitutions
# that never change the underlying word, number or name.
_QUOTE_DASH_MAP = {
    # double quotes: " " „ ‟ « » ″
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2033": '"',
    # single quotes / apostrophes: ' ' ‚ ‛ ‹ › ′ `
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u2039": "'",
    "\u203a": "'",
    "\u2032": "'",
    "\u0060": "'",
    # dashes / minus: ‐ ‑ ‒ – — ― −
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
}
_QUOTE_DASH_TABLE = str.maketrans(_QUOTE_DASH_MAP)

# Numbers (incl. dates/scores/percentages/decimals) and capitalised names — used
# only to build human-readable diagnostics when the strict comparison fails.
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,:%\-]\d+)*(?!\w)")
_NAME_RE = re.compile(r"(?<![.!?]\s)\b[A-ZÇĞİÖŞÜ][A-Za-zÇĞİÖŞÜçğıöşü'\-]{2,}\b")


def normalize_narration_text(text: str) -> str:
    """Apply the narrow, defined technical normalizations for lock comparison.

    See the module docstring for the exhaustive list. Nothing here changes a
    digit, a word, a name, casing or ordering.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", str(text))
    out = out.translate(_QUOTE_DASH_TABLE)
    # Collapse every run of whitespace (spaces, tabs, newlines) to one space.
    out = re.sub(r"\s+", " ", out)
    # Remove whitespace directly before common punctuation (spacing only).
    out = re.sub(r"\s+([,.;:!?…])", r"\1", out)
    return out.strip()


def _narration_text_of(chapter) -> str:
    """Extract narration text from a Chapter model or a plain dict."""
    narration = getattr(chapter, "narration", None)
    if narration is None and isinstance(chapter, dict):
        narration = chapter.get("narration")
    if narration is None:
        return ""
    text = getattr(narration, "text", None)
    if text is None and isinstance(narration, dict):
        text = narration.get("text", "")
    return text or ""


def compose_chapter_narration(chapters) -> str:
    """Concatenate chapter narration in the given (chapter) order.

    Chapters are joined with a newline; normalization collapses the separator so
    the join character never affects the comparison — only the ordered content
    matters.
    """
    return "\n".join(_narration_text_of(ch) for ch in chapters)


def restore_minor_narration_drift(
    approved_text: str,
    chapters,
    *,
    max_changed_characters: int = 5,
) -> bool:
    """Restore exact approved text when chapter boundaries are already trustworthy.

    This only repairs equal-length, character-level model drift and refuses
    insertions, deletions, broad rewrites, or boundaries that no longer align
    with spaces in the approved narration.
    """
    approved = normalize_narration_text(approved_text)
    composed = normalize_narration_text(compose_chapter_narration(chapters))
    if len(approved) != len(composed):
        return False
    changed = sum(left != right for left, right in zip(approved, composed, strict=True))
    if changed == 0 or changed > max_changed_characters:
        return False

    lengths = [len(normalize_narration_text(_narration_text_of(chapter))) for chapter in chapters]
    restored: list[str] = []
    cursor = 0
    for index, length in enumerate(lengths):
        restored.append(approved[cursor : cursor + length])
        cursor += length
        if index < len(lengths) - 1:
            if approved[cursor : cursor + 1] != " " or composed[cursor : cursor + 1] != " ":
                return False
            cursor += 1
    if cursor != len(approved):
        return False

    for chapter, text in zip(chapters, restored, strict=True):
        narration = getattr(chapter, "narration", None)
        if narration is None and isinstance(chapter, dict):
            narration = chapter.get("narration")
        if isinstance(narration, dict):
            narration["text"] = text
        else:
            narration.text = text
    return check_narration_lock(approved, compose_chapter_narration(chapters)).matches


@dataclass
class NarrationLockResult:
    """Outcome of comparing composed chapter narration to approved narration."""

    matches: bool
    approved_normalized: str
    composed_normalized: str
    first_divergence_index: int | None = None
    approved_excerpt: str = ""
    composed_excerpt: str = ""
    added_numbers: list[str] | None = None
    removed_numbers: list[str] | None = None
    added_names: list[str] | None = None
    removed_names: list[str] | None = None

    def summary(self) -> str:
        if self.matches:
            return "narration preserved"
        parts: list[str] = []
        if self.removed_numbers:
            parts.append(f"removed/changed numbers: {sorted(self.removed_numbers)}")
        if self.added_numbers:
            parts.append(f"added numbers: {sorted(self.added_numbers)}")
        if self.removed_names:
            parts.append(f"removed names: {sorted(self.removed_names)}")
        if self.added_names:
            parts.append(f"added names: {sorted(self.added_names)}")
        if self.first_divergence_index is not None:
            parts.append(
                f"first divergence at char {self.first_divergence_index}: "
                f"approved={self.approved_excerpt!r} composed={self.composed_excerpt!r}"
            )
        if not parts:
            parts.append(
                "normalized narration length "
                f"{len(self.approved_normalized)}→{len(self.composed_normalized)}"
            )
        return "; ".join(parts)


def _first_divergence(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    return limit


def _multiset_delta(
    pattern: re.Pattern, approved: str, composed: str
) -> tuple[list[str], list[str]]:
    approved_counts = Counter(pattern.findall(approved))
    composed_counts = Counter(pattern.findall(composed))
    added = list((composed_counts - approved_counts).elements())
    removed = list((approved_counts - composed_counts).elements())
    return added, removed


def check_narration_lock(approved_text: str, composed_text: str) -> NarrationLockResult:
    """Compare approved narration against composed chapter narration.

    Returns a :class:`NarrationLockResult`. ``matches`` is True only when the two
    texts are identical after the defined technical normalizations.
    """
    approved_norm = normalize_narration_text(approved_text)
    composed_norm = normalize_narration_text(composed_text)
    if approved_norm == composed_norm:
        return NarrationLockResult(
            matches=True,
            approved_normalized=approved_norm,
            composed_normalized=composed_norm,
        )

    idx = _first_divergence(approved_norm, composed_norm)
    added_numbers, removed_numbers = _multiset_delta(_NUMBER_RE, approved_norm, composed_norm)
    added_names, removed_names = _multiset_delta(_NAME_RE, approved_norm, composed_norm)
    window = 40
    return NarrationLockResult(
        matches=False,
        approved_normalized=approved_norm,
        composed_normalized=composed_norm,
        first_divergence_index=idx,
        approved_excerpt=approved_norm[idx : idx + window],
        composed_excerpt=composed_norm[idx : idx + window],
        added_numbers=added_numbers,
        removed_numbers=removed_numbers,
        added_names=added_names,
        removed_names=removed_names,
    )
