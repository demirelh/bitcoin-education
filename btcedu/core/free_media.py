"""Freely licensed library pictures for video chapters.

Before a chapter gets a generated or stock picture, the curated collection
(:mod:`btcedu.core.editorial.media_collection`) and the Wikidata portrait
lookup are asked for a real photograph of the person, place or topic the
chapter is about. Rights are judged by the same ``assess_candidate`` the
newsroom uses. The only edit is neutral: the photograph is fitted into 16:9 on
a blurred copy of itself, and that edit is part of the credit line.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FRAME = (1920, 1080)
EDIT_NOTE = "16:9 formatına bulanık arka planla yerleştirildi"


def chapter_fields(chapter) -> list[tuple[str, int]]:
    """Headline first, then the Turkish narration, then the German source."""
    narration = getattr(getattr(chapter, "narration", None), "text", "") or ""
    return [
        (getattr(chapter, "display_headline", None) or chapter.title or "", 3),
        (narration, 2),
        (getattr(chapter, "source_text", None) or "", 1),
    ]


def fit_16x9(body: bytes) -> bytes:
    """Whole picture, centred, over a blurred and darkened fill of itself."""
    from PIL import Image, ImageEnhance, ImageFilter

    width, height = FRAME
    with Image.open(io.BytesIO(body)) as source:
        picture = source.convert("RGB")
    fill = picture.copy()
    scale = max(width / fill.width, height / fill.height)
    fill = fill.resize((round(fill.width * scale), round(fill.height * scale)))
    left, top = (fill.width - width) // 2, (fill.height - height) // 2
    fill = fill.crop((left, top, left + width, top + height))
    fill = ImageEnhance.Brightness(fill.filter(ImageFilter.GaussianBlur(40))).enhance(0.55)
    scale = min(width / picture.width, height / picture.height)
    picture = picture.resize((round(picture.width * scale), round(picture.height * scale)))
    fill.paste(picture, ((width - picture.width) // 2, (height - picture.height) // 2))
    out = io.BytesIO()
    fill.save(out, format="JPEG", quality=90)
    return out.getvalue()


def find_free_picture(chapter, output_dir: Path, settings, filename_stem: str) -> dict | None:
    """Download, check and frame a cleared picture, or ``None`` when there is none."""
    from btcedu.core.editorial.media import (
        ALLOWED_IMAGE_TYPES,
        MAX_IMAGE_BYTES,
        assess_candidate,
        build_attribution,
    )
    from btcedu.core.editorial.media_collection import (
        CollectionCommonsProvider,
        WikidataPortraitLookup,
        match_collection,
        requirement_for,
        select_entry,
    )
    from btcedu.services.document_fetcher import DocumentFetcher

    fetcher = DocumentFetcher.from_settings(settings)
    lookup = WikidataPortraitLookup(
        fetcher, Path(settings.outputs_dir).parent / "media-collection-learned.json"
    )
    fields = chapter_fields(chapter)
    headline_entry = match_collection(fields[:1])
    entry = headline_entry or select_entry(fields, lookup)
    if entry is None or (entry is not headline_entry and entry.kind != "person"):
        # A place or topic named only in passing is too loose for the one picture
        # a chapter shows; a fuel-price sign once illustrated an EV subsidy.
        return None
    candidates = CollectionCommonsProvider(fetcher, extra=(entry,)).search(entry.label, limit=1)
    if not candidates:
        return None
    candidate = candidates[0]
    requirement = requirement_for(entry)
    assessment = assess_candidate(candidate, requirement)
    if not assessment.eligible:
        logger.info("Free picture %s rejected: %s", entry.key, "; ".join(assessment.reasons))
        return None
    binary = fetcher.fetch_binary(
        candidate.file_url, allowed_content_types=ALLOWED_IMAGE_TYPES, max_bytes=MAX_IMAGE_BYTES
    )
    framed = fit_16x9(binary.body)
    filename = f"{filename_stem}_commons.jpg"
    (output_dir / filename).write_bytes(framed)
    return {
        "filename": filename,
        "size_bytes": len(framed),
        "label": entry.label,
        "collection_key": entry.key,
        "role": entry.role.value,
        "attribution": build_attribution(candidate, assessment.policy, edit_note=EDIT_NOTE),
        "license": assessment.policy.license_id,
        "license_url": candidate.license_url,
        "source_page": candidate.page_url,
        "author": candidate.author,
    }
