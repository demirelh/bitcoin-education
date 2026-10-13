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
PAIR_EDIT_NOTE = "iki fotoğraf yan yana, bulanık arka planla yerleştirildi"


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


def fit_pair(left: bytes, right: bytes) -> bytes:
    """Two pictures side by side, each whole on a blurred fill of itself."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    width, height = FRAME
    half = width // 2
    canvas = Image.new("RGB", FRAME)
    for index, body in enumerate((left, right)):
        with Image.open(io.BytesIO(body)) as source:
            picture = source.convert("RGB")
        scale = max(half / picture.width, height / picture.height)
        fill = picture.resize((round(picture.width * scale), round(picture.height * scale)))
        x0, y0 = (fill.width - half) // 2, (fill.height - height) // 2
        fill = fill.crop((x0, y0, x0 + half, y0 + height)).filter(ImageFilter.GaussianBlur(40))
        fill = ImageEnhance.Brightness(fill).enhance(0.55)
        scale = min(half / picture.width, height / picture.height)
        picture = picture.resize((round(picture.width * scale), round(picture.height * scale)))
        fill.paste(picture, ((half - picture.width) // 2, (height - picture.height) // 2))
        canvas.paste(fill, (index * half, 0))
    ImageDraw.Draw(canvas).rectangle((half - 3, 0, half + 2, height), fill=(255, 255, 255))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90)
    return out.getvalue()


def find_free_picture(
    chapter,
    output_dir: Path,
    settings,
    filename_stem: str,
    *,
    exclude: frozenset[str] = frozenset(),
    alternate: bool = False,
) -> dict | None:
    """Download, check and frame a cleared picture, or ``None`` when there is none.

    *exclude* holds Commons files the episode already shows. A person whose
    pinned portrait is excluded, or for whom *alternate* asks for another shot,
    is shown in a further photograph that depicts them (Commons P180).
    """
    from btcedu.core.editorial.media import (
        ALLOWED_IMAGE_TYPES,
        MAX_IMAGE_BYTES,
        assess_candidate,
        build_attribution,
    )
    from btcedu.core.editorial.media_collection import (
        CollectionCommonsProvider,
        WikidataPortraitLookup,
        depicted_photos,
        match_collection,
        persons_named,
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
    pinned = (
        ()
        if alternate
        else CollectionCommonsProvider(fetcher, extra=(entry,)).search(entry.label, limit=1)
    )
    requirement = requirement_for(entry)

    def _usable(pool):
        for candidate in pool:
            if candidate.offer_key in exclude:
                continue
            assessment = assess_candidate(candidate, requirement)
            if assessment.eligible:
                return candidate, assessment
            logger.info("Free picture %s rejected: %s", entry.key, "; ".join(assessment.reasons))
        return None

    found = _usable(pinned) or _usable(depicted_photos(fetcher, entry))
    if found is None:
        return None
    candidate, assessment = found
    if not alternate and entry.kind == "person":
        caption = f"{fields[0][0]} {getattr(chapter, 'display_summary', None) or ''}"
        named = persons_named(caption)
        partner = next((p for p in named if p.key != entry.key), None)
        if partner is not None and entry in named:
            pair = _pair_picture(
                fetcher,
                (entry, candidate, assessment),
                partner,
                exclude,
                output_dir,
                filename_stem,
                left_first=named.index(entry) < named.index(partner),
            )
            if pair is not None:
                return pair
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
        "commons_file": candidate.offer_key,
    }


def _pair_picture(fetcher, first, partner, exclude, output_dir, filename_stem, *, left_first):
    """Both people a story is about, side by side, or None if the partner has no cleared photo."""
    from btcedu.core.editorial.media import (
        ALLOWED_IMAGE_TYPES,
        MAX_IMAGE_BYTES,
        assess_candidate,
        build_attribution,
    )
    from btcedu.core.editorial.media_collection import CollectionCommonsProvider, requirement_for

    found = CollectionCommonsProvider(fetcher, extra=(partner,)).search(partner.label, limit=1)
    if not found or found[0].offer_key in exclude:
        return None
    partner_assessment = assess_candidate(found[0], requirement_for(partner))
    if not partner_assessment.eligible:
        return None
    sides = [first, (partner, found[0], partner_assessment)]
    if not left_first:
        sides.reverse()
    bodies = [
        fetcher.fetch_binary(
            candidate.file_url, allowed_content_types=ALLOWED_IMAGE_TYPES, max_bytes=MAX_IMAGE_BYTES
        ).body
        for _, candidate, _ in sides
    ]
    framed = fit_pair(*bodies)
    filename = f"{filename_stem}_commons.jpg"
    (output_dir / filename).write_bytes(framed)
    return {
        "filename": filename,
        "size_bytes": len(framed),
        "label": " & ".join(entry.label for entry, _, _ in sides),
        "collection_key": first[0].key,
        "role": first[0].role.value,
        "attribution": "; ".join(
            build_attribution(candidate, assessment.policy, edit_note=PAIR_EDIT_NOTE)
            for _, candidate, assessment in sides
        ),
        "license": " / ".join(
            dict.fromkeys(assessment.policy.license_id for _, _, assessment in sides)
        ),
        "license_url": sides[0][1].license_url,
        "source_page": sides[0][1].page_url,
        "author": "; ".join(filter(None, (candidate.author for _, candidate, _ in sides))),
        "commons_file": "|".join(candidate.offer_key for _, candidate, _ in sides),
    }
