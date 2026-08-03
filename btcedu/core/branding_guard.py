"""Branding guard: keep foreign source attribution out of the visible video.

The broadcast is an independent Turkish-language news programme. Its own brand
belongs on screen; the upstream broadcaster, the internal project name, profile
names, episode IDs, provider/model names and internal paths must not.

Source provenance itself is *not* removed — it stays in the story documents, QA
artifacts, provenance files and (optionally) the YouTube description. Only the
**visible** layer is guarded.

The guard runs twice:

* before ``render`` — so a bad chapter document never reaches ffmpeg
* before ``publish`` — so a manually edited artifact cannot slip through
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from btcedu.config import Settings

logger = logging.getLogger(__name__)

# Terms that must never appear in the visible video for any profile. Kept
# deliberately small and specific: these are attribution/branding leaks, not a
# general profanity filter.
DEFAULT_FORBIDDEN_VISIBLE_TERMS: tuple[str, ...] = (
    "ARD Tagesschau",
    "ARD tagesschau",
    "Das Erste",
    "btcedu",
    "Kaynak:",
    "Quelle:",
    "tagesschau",
)

# Structural patterns for technical leakage (episode IDs, paths, model names).
_TECHNICAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal_path", re.compile(r"(?:^|\s)(?:/home/|/data/|data/outputs/|\./)\S+")),
    (
        "provider_or_model",
        re.compile(
            r"\b(?:elevenlabs|claude-[\w.-]+|gpt-[\w.-]+|gemini-[\w.-]+|dall-?e|ideogram|flux|pexels)\b",
            re.IGNORECASE,
        ),
    ),
    ("profile_name", re.compile(r"\b[a-z0-9]+_(?:tr|de|en)\b")),
)

# A YouTube-style ID is 11 chars of [A-Za-z0-9_-]. Matching that alone is far
# too greedy for Turkish prose, so it is only applied to short label-like texts.
_EPISODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class BrandingViolation:
    """A single forbidden term found in a visible text."""

    location: str
    reason: str
    term: str
    excerpt: str

    def describe(self) -> str:
        return f"{self.location}: {self.reason} {self.term!r} in {self.excerpt!r}"


@dataclass
class VisibleText:
    """A piece of text that ends up on screen."""

    location: str
    text: str


@dataclass
class BrandingScanResult:
    """Outcome of a visible-text scan."""

    violations: list[BrandingViolation] = field(default_factory=list)
    scanned: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        if self.ok:
            return f"no forbidden visible text in {self.scanned} texts"
        return "; ".join(v.describe() for v in self.violations)


def branding_config(settings: Settings, profile_name: str | None) -> dict[str, Any]:
    """Return the profile's ``branding`` block (empty dict when unavailable)."""
    if not profile_name:
        return {}
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(profile_name)
    except Exception:  # profile registry unavailable or unknown profile
        return {}
    return dict(getattr(profile, "branding", {}) or {})


def forbidden_terms(branding: dict[str, Any]) -> list[str]:
    """Forbidden visible terms for a branding block.

    Returns an empty list when the profile explicitly allows visible source
    attribution, so existing profiles keep their current behaviour.
    """
    if not branding:
        return []
    if branding.get("visible_source_attribution", True):
        return []
    configured = branding.get("forbidden_visible_terms")
    if configured is None:
        terms = list(DEFAULT_FORBIDDEN_VISIBLE_TERMS)
    else:
        terms = [str(t) for t in configured if str(t).strip()]
    # The programme's own name must stay allowed even if it contains a term.
    return terms


def _allowed_texts(branding: dict[str, Any]) -> tuple[str, ...]:
    allowed = []
    for key in ("show_name", "slogan"):
        value = branding.get(key)
        if value:
            allowed.append(str(value))
    return tuple(allowed)


def _mask_allowed(text: str, allowed: tuple[str, ...]) -> str:
    masked = text
    for phrase in allowed:
        masked = masked.replace(phrase, " ")
    return masked


def scan_texts(
    texts: list[VisibleText],
    branding: dict[str, Any],
    *,
    check_technical: bool = True,
) -> BrandingScanResult:
    """Scan visible texts for forbidden attribution and technical leakage."""
    result = BrandingScanResult(scanned=len(texts))
    terms = forbidden_terms(branding)
    if not terms and not branding:
        return result
    allowed = _allowed_texts(branding)

    for item in texts:
        raw = (item.text or "").strip()
        if not raw:
            continue
        candidate = _mask_allowed(raw, allowed)
        lowered = candidate.lower()
        for term in terms:
            if term.lower() in lowered:
                result.violations.append(
                    BrandingViolation(
                        location=item.location,
                        reason="forbidden source attribution",
                        term=term,
                        excerpt=raw[:120],
                    )
                )
                break
        if not check_technical:
            continue
        # Technical-leakage heuristics are label-oriented. Long-form narration is
        # prose and would produce false positives, so it is only checked for
        # attribution terms above.
        if item.location.endswith(".narration"):
            continue
        for reason, pattern in _TECHNICAL_PATTERNS:
            match = pattern.search(candidate)
            if match:
                result.violations.append(
                    BrandingViolation(
                        location=item.location,
                        reason=f"technical leakage ({reason})",
                        term=match.group(0).strip(),
                        excerpt=raw[:120],
                    )
                )
                break
        if _EPISODE_ID_PATTERN.match(candidate.strip()):
            result.violations.append(
                BrandingViolation(
                    location=item.location,
                    reason="technical leakage (episode_id)",
                    term=candidate.strip(),
                    excerpt=raw[:120],
                )
            )

    return result


def collect_chapter_texts(chapter_doc: Any) -> list[VisibleText]:
    """Collect every on-screen text of a chapter document.

    Includes chapter titles, overlay texts (both lines) and narration — the
    narration is spoken by TTS and is therefore audible rather than visible, but
    an attribution sentence read aloud is exactly as unwanted.
    """
    texts: list[VisibleText] = []
    texts.append(VisibleText("document.title", getattr(chapter_doc, "title", "") or ""))
    for chapter in getattr(chapter_doc, "chapters", []) or []:
        cid = getattr(chapter, "chapter_id", "?")
        texts.append(VisibleText(f"{cid}.title", getattr(chapter, "title", "") or ""))
        for attr in ("display_headline", "display_summary"):
            value = getattr(chapter, attr, None)
            if value:
                texts.append(VisibleText(f"{cid}.{attr}", str(value)))
        narration = getattr(chapter, "narration", None)
        if narration is not None:
            texts.append(VisibleText(f"{cid}.narration", getattr(narration, "text", "") or ""))
        for index, overlay in enumerate(getattr(chapter, "overlays", []) or []):
            texts.append(VisibleText(f"{cid}.overlay[{index}].text", getattr(overlay, "text", "")))
            subtext = getattr(overlay, "subtext", None)
            if subtext:
                texts.append(VisibleText(f"{cid}.overlay[{index}].subtext", str(subtext)))
    return texts


def collect_render_texts(render_config: dict[str, Any]) -> list[VisibleText]:
    """Collect on-screen texts configured for the renderer (intro/outro/ticker)."""
    texts: list[VisibleText] = []
    for key in (
        "intro_show_name",
        "intro_slogan",
        "intro_episode_title",
        "topic_intro_label",
        "outro_text",
        "ticker_text",
    ):
        value = (render_config or {}).get(key)
        if value:
            texts.append(VisibleText(f"render.{key}", str(value)))
    return texts


def sanitize_overlays(chapter_doc: Any, branding: dict[str, Any]) -> int:
    """Drop overlays whose text carries forbidden attribution.

    Used for backward compatibility: chapter documents produced before this
    feature contain a mandatory attribution lower third. Removing it silently is
    preferable to failing an otherwise valid, already-approved episode.

    Returns the number of removed overlays.
    """
    terms = forbidden_terms(branding)
    if not terms:
        return 0
    allowed = _allowed_texts(branding)
    removed = 0
    for chapter in getattr(chapter_doc, "chapters", []) or []:
        kept = []
        for overlay in getattr(chapter, "overlays", []) or []:
            haystack = _mask_allowed(
                f"{getattr(overlay, 'text', '')} {getattr(overlay, 'subtext', '') or ''}", allowed
            ).lower()
            if any(term.lower() in haystack for term in terms):
                removed += 1
                continue
            kept.append(overlay)
        if removed:
            chapter.overlays = kept
    return removed


def assert_no_forbidden_visible_text(
    settings: Settings,
    episode_id: str,
    profile_name: str | None,
    chapter_doc: Any,
    *,
    render_config: dict[str, Any] | None = None,
    stage: str = "render",
) -> BrandingScanResult:
    """Fail closed when forbidden text would become visible.

    Raises:
        PipelineError: with ``PERMANENT_CONTENT`` when a violation is found.
    """
    branding = branding_config(settings, profile_name)
    if not branding or branding.get("visible_source_attribution", True):
        return BrandingScanResult()

    texts = collect_chapter_texts(chapter_doc)
    if render_config:
        texts.extend(collect_render_texts(render_config))
    result = scan_texts(texts, branding)
    _write_report(settings, episode_id, stage, result)
    if not result.ok:
        from btcedu.services.errors import ErrorCategory, PipelineError

        raise PipelineError(
            f"Branding guard blocked {stage} for {episode_id}: {result.summary()}",
            ErrorCategory.PERMANENT_CONTENT,
        )
    return result


def _write_report(
    settings: Settings, episode_id: str, stage: str, result: BrandingScanResult
) -> None:
    import json

    path = Path(settings.outputs_dir) / episode_id / "provenance" / f"branding_guard_{stage}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "stage": stage,
                    "episode_id": episode_id,
                    "ok": result.ok,
                    "scanned_texts": result.scanned,
                    "violations": [
                        {
                            "location": v.location,
                            "reason": v.reason,
                            "term": v.term,
                            "excerpt": v.excerpt,
                        }
                        for v in result.violations
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as error:  # diagnostics must never break the pipeline
        logger.warning("Could not write branding guard report: %s", error)
