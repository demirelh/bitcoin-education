"""Provider-neutral catalogue contracts for freely licensed media candidates.

A catalogue entry is a claim about rights, never a guarantee. This module only
reports what a catalogue said; whether the newsroom may use the picture is
decided in :mod:`btcedu.core.editorial.media`.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"


@dataclass(frozen=True)
class MediaCandidate:
    """One catalogue offer, already stripped of catalogue markup."""

    provider: str
    offer_key: str
    page_url: str
    file_url: str
    title: str = ""
    description: str = ""
    uploader: str | None = None
    author: str | None = None
    credit: str | None = None
    license_id: str = ""
    license_version: str | None = None
    license_url: str | None = None
    captured_at_text: str | None = None
    captured_at: datetime | None = None
    depicted_subject: str | None = None
    depicted_location: str | None = None
    mime_type: str = ""
    width: int | None = None
    height: int | None = None
    byte_size: int | None = None
    categories: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)


class MediaCatalogProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> tuple[MediaCandidate, ...]: ...


class MediaCatalogError(RuntimeError):
    pass


class FixtureCommonsProvider:
    """Deterministic catalogue used by tests and offline editorial fixtures."""

    name = "commons_fixture"

    def __init__(self, responses: Mapping[str, tuple[MediaCandidate, ...]]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int) -> tuple[MediaCandidate, ...]:
        self.calls.append((query, limit))
        return tuple(self.responses.get(query, ())[:limit])


class WikimediaCommonsProvider:
    """Reads the Commons API through the one controlled newsroom fetcher."""

    name = "wikimedia_commons"

    def __init__(self, fetcher, *, api_url: str = COMMONS_API_URL) -> None:
        self.fetcher = fetcher
        self.api_url = api_url

    def search(self, query: str, *, limit: int) -> tuple[MediaCandidate, ...]:
        from urllib.parse import urlencode

        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrnamespace": "6",
            "gsrlimit": str(max(1, min(limit, 50))),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata|user",
        }
        document = self.fetcher.fetch(f"{self.api_url}?{urlencode(params)}")
        try:
            payload = json.loads(document.text or document.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MediaCatalogError("Commons returned an unreadable response") from exc
        pages = payload.get("query", {}).get("pages", []) or []
        candidates = []
        for page in pages:
            candidate = self._to_candidate(page)
            if candidate is not None:
                candidates.append(candidate)
        return tuple(candidates[:limit])

    def _to_candidate(self, page: Mapping) -> MediaCandidate | None:
        infos = page.get("imageinfo") or []
        if not infos:
            return None
        info = infos[0]
        file_url = info.get("url")
        page_url = info.get("descriptionurl")
        if not file_url or not page_url:
            return None
        extmetadata = {
            key: sanitize_catalog_html(str((value or {}).get("value", "")))
            for key, value in (info.get("extmetadata") or {}).items()
        }
        license_id = extmetadata.get("LicenseShortName") or extmetadata.get("License") or ""
        return MediaCandidate(
            provider=self.name,
            offer_key=str(page.get("title") or page_url),
            page_url=page_url,
            file_url=file_url,
            title=sanitize_catalog_html(str(page.get("title") or "")),
            description=extmetadata.get("ImageDescription", ""),
            uploader=info.get("user"),
            author=extmetadata.get("Artist") or None,
            credit=extmetadata.get("Credit") or None,
            license_id=license_id,
            license_version=extmetadata.get("LicenseVersion") or None,
            license_url=extmetadata.get("LicenseUrl") or None,
            captured_at_text=extmetadata.get("DateTimeOriginal") or None,
            captured_at=parse_catalog_datetime(extmetadata.get("DateTimeOriginal")),
            mime_type=info.get("mime") or "",
            width=info.get("width"),
            height=info.get("height"),
            byte_size=info.get("size"),
            metadata=extmetadata,
        )


class _TextExtractor(HTMLParser):
    """Collects visible text and discards script and style content entirely."""

    _DROPPED = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._DROPPED:
            self._suppressed += 1

    def handle_endtag(self, tag):
        if tag in self._DROPPED and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data):
        if not self._suppressed:
            self.parts.append(data)


def sanitize_catalog_html(value: str) -> str:
    """Reduce catalogue markup to plain text.

    Catalogue fields are user-supplied HTML. Keeping the markup would let a
    caption smuggle a link or script into the credit line of a published page,
    so only the visible text survives.
    """
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\s+", " ", text).strip()


def parse_catalog_datetime(value: str | None) -> datetime | None:
    """Read a catalogue date, or report that there is none.

    A missing or unreadable capture date must stay missing: guessing one would
    let an archive photo pass as a picture of the current event.
    """
    if not value:
        return None
    text = value.strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
