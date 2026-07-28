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
  * ellipsis runs (``...`` / ``…``) → a word boundary
  * whitespace / newline runs collapsed to a single space
  * whitespace removed *before* punctuation (punctuation spacing)

Explicitly NOT normalized: digits, letters/words, names, casing or ordering.
A changed digit, a dropped name, an added sentence or a reordered paragraph all
produce a mismatch and fail the lock closed.
"""

from __future__ import annotations

import difflib
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
    # Ellipses mark a pause rather than spoken content. Models commonly replace
    # a leading continuation marker ("...tekrar") with a plain word boundary.
    # Treat both forms identically without weakening checks for words, numbers,
    # names, sentence punctuation, or ordering.
    out = re.sub(r"(?:\.{2,}|…+)", " ", out)
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


def _set_narration_text(chapter, text: str) -> None:
    narration = getattr(chapter, "narration", None)
    if narration is None and isinstance(chapter, dict):
        narration = chapter.get("narration")
    if isinstance(narration, dict):
        narration["text"] = text
    else:
        narration.text = text


def restore_truncated_narration_suffix(
    approved_text: str,
    chapters,
    *,
    max_missing_ratio: float = 0.1,
    max_missing_characters: int = 2_000,
) -> bool:
    """Restore an exact approved suffix omitted from the final chapter.

    This repair is allowed only when the composed narration is an exact,
    substantial prefix of the approved narration. It never asks a model to
    regenerate approved words and cannot alter existing chapter narration.
    """
    approved = normalize_narration_text(approved_text)
    composed = normalize_narration_text(compose_chapter_narration(chapters))
    if not approved or not composed or not chapters or not approved.startswith(composed):
        return False

    missing = approved[len(composed) :]
    if not missing or len(missing) > max_missing_characters:
        return False
    if len(missing) / len(approved) > max_missing_ratio:
        return False

    final_chapter = chapters[-1]
    current_text = _narration_text_of(final_chapter).rstrip()
    _set_narration_text(final_chapter, current_text + missing)
    return check_narration_lock(approved, compose_chapter_narration(chapters)).matches


def restore_narration_from_approved(
    approved_text: str,
    chapters,
    *,
    max_omitted_ratio: float = 0.5,
) -> bool:
    """Rebuild chapter narration from the approved narration when the composed
    narration is a faithful *subsequence* of it.

    Chapterization models frequently re-case the approved narration (e.g. lower
    a sentence-initial word that the approved text capitalised mid-flow) and may
    omit words when the JSON output hits the token budget. Both are safe to undo
    deterministically: the approved narration is the QA-gated source of truth, so
    we restore the exact approved words — every fact, number, name and casing —
    partitioned along the chapter boundaries the model proposed. The lock then
    passes by construction.

    This is intentionally strict. It accepts ONLY when the composed narration is
    an order-preserving subsequence of the approved narration under case folding,
    i.e. the model *only* re-cased and/or dropped words. It refuses (returns
    False → fail closed) on any inserted word (hallucination) or changed
    word/number/name (a real content edit), which are precisely the cases that
    warrant human review — never silently "repaired".
    """
    approved = normalize_narration_text(approved_text)
    if not approved or not chapters:
        return False
    approved_words = approved.split()
    chapter_word_lists = [
        normalize_narration_text(_narration_text_of(chapter)).split() for chapter in chapters
    ]
    if any(len(words) == 0 for words in chapter_word_lists):
        return False
    composed_words = [word for words in chapter_word_lists for word in words]
    if not composed_words or len(composed_words) > len(approved_words):
        return False

    omitted = len(approved_words) - len(composed_words)
    if approved_words and omitted / len(approved_words) > max_omitted_ratio:
        return False

    matcher = difflib.SequenceMatcher(
        a=[word.casefold() for word in composed_words],
        b=[word.casefold() for word in approved_words],
        autojunk=False,
    )
    composed_to_approved: list[int | None] = [None] * len(composed_words)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # 'a' is composed, 'b' is approved.
        #   equal   → composed word matches approved (possibly different casing)
        #   insert  → approved has extra words (model omission — allowed, restored)
        #   delete  → composed has words absent from approved (hallucination)
        #   replace → composed word differs from approved (changed content)
        if tag in ("delete", "replace"):
            return False
        if tag == "equal":
            for offset in range(i2 - i1):
                composed_to_approved[i1 + offset] = j1 + offset
    if any(index is None for index in composed_to_approved):
        return False

    # Each chapter owns the approved span from its first composed word up to the
    # next chapter's first composed word; omitted approved words fall inside a
    # span and are absorbed, so concatenation reproduces the approved narration.
    starts: list[int] = []
    prev = 0
    cursor = 0
    for chapter_index, words in enumerate(chapter_word_lists):
        start = 0 if chapter_index == 0 else max(composed_to_approved[cursor], prev)
        starts.append(start)
        prev = start
        cursor += len(words)

    for chapter_index, chapter in enumerate(chapters):
        start = starts[chapter_index]
        end = starts[chapter_index + 1] if chapter_index + 1 < len(starts) else len(approved_words)
        end = max(end, start)
        _set_narration_text(chapter, " ".join(approved_words[start:end]))

    return check_narration_lock(approved_text, compose_chapter_narration(chapters)).matches


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
