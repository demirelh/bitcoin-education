"""Search over free, key-less endpoints, for the unattended newsroom run.

No provider contract exists for the development newsroom, and a search API
that needs one is exactly the sort of thing that must not silently start
billing an unattended timer. These two endpoints are public, unauthenticated
and documented:

* ``tagesschau.de/api2u/search`` — the broadcaster's own article archive.
* the MediaWiki search API — an independent publisher, useful for the
  background and dates a broadcast mentions in passing.

Two publishers is the minimum that makes source independence a real question
rather than a formality. It is not a substitute for a general web index, and
the daily report says so: a claim that only tagesschau reports will be
assessed against tagesschau, and the evidence record will show that.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from btcedu.services.search_service import SearchHit, SearchProviderError, SearchResponse

TAGESSCHAU_SEARCH = "https://www.tagesschau.de/api2u/search/"
WIKIPEDIA_SEARCH = "https://de.wikipedia.org/w/api.php"

#: Result types the archive returns that are not readable articles. A video
#: page carries a headline and no body, so fetching it yields no passage that
#: could support anything.
_UNREADABLE = frozenset({"video", "audio", "bildergalerie"})

#: German function words carry no retrieval value but are matched literally by
#: both endpoints, so they actively reduce a query to nothing.
_STOPWORDS = frozenset(
    """
    aber alle als am an auch auf aus bei beim bis das dass dem den der des die
    durch ein eine einem einen einer eines er es fuer für gegen hat haben hier
    ihr im in ist ins kann mit nach nicht noch oder ohne sein sich sie sind so
    soll über um und uns unter vom von vor war werden wie wird wurde zu zum zur
    """.split()
)


#: Terms that survive the capitalised-noun filter but say nothing about what a
#: story is *about*. Dates, counts and currency nouns appear in almost every
#: German news sentence, so counting them as topical overlap lets an unrelated
#: document look relevant purely by sharing a calendar word.
_GENERIC = frozenset(
    """
    jahr jahre jahren jahrzehnt mal monat monate monaten woche wochen tag tage
    tagen stunde stunden uhr minute minuten prozent euro cent dollar million
    millionen milliarde milliarden anfang ende mitte montag dienstag mittwoch
    donnerstag freitag samstag sonntag januar februar maerz märz april juni
    juli august september oktober november dezember zahl zahlen teil teile
    """.split()
)

#: How many distinct topic terms a result has to share with the query before it
#: counts as being about the same story. One is not enough: a single shared
#: word is exactly how an encyclopedia article about an Italian art gallery
#: ended up offered as evidence for a German department-store insolvency.
_MIN_TOPIC_TERMS = 2

#: Compound-tolerant matching: German glues nouns together, so "Rekordpreise"
#: in a query should still match "Rekord" in a headline.
_STEM = 6


def _keywords(query: str, limit: int) -> str:
    """Reduce a query to the terms that actually carry it.

    Both endpoints are keyword searches that require every term to match, so a
    full headline retrieves nothing. German capitalises its nouns, which makes
    the carrying terms cheap to identify without a language model.
    """
    words = re.findall(r"[\wÄÖÜäöüß-]+", query)
    kept: list[str] = []
    for word in words:
        if word.lower() in _STOPWORDS or len(word) < 3:
            continue
        if word[0].isupper() or word.isdigit():
            kept.append(word)
    if not kept:
        kept = [w for w in words if w.lower() not in _STOPWORDS and len(w) >= 4]
    return " ".join(kept[:limit])


def _topic_terms(query: str) -> set[str]:
    """The distinct terms a result has to share to be about the same story.

    Hyphenated compounds are split, because "Galleria-Filialen" retrieves
    documents that contain only one of the two halves.
    """
    terms: set[str] = set()
    for word in _keywords(query, limit=12).split():
        for part in word.split("-"):
            part = part.strip().lower()
            if len(part) >= 3 and part not in _STOPWORDS and part not in _GENERIC:
                terms.add(part)
    return terms


def _matched_terms(item: _Item, terms: set[str]) -> set[str]:
    """Which topic terms the result's own title and summary actually contain.

    Matching is on whole words with a compound-tolerant prefix, never on raw
    substrings: "Ketterer" must not be read as a hit for "Kette".
    """
    haystack = f"{item.title} {item.snippet}".lower()
    words = set(re.findall(r"[\wäöüß]+", haystack))
    matched: set[str] = set()
    for term in terms:
        for word in words:
            if word == term:
                matched.add(term)
                break
            if len(term) >= _STEM and word.startswith(term[:_STEM]):
                matched.add(term)
                break
            if len(word) >= _STEM and term.startswith(word[:_STEM]):
                matched.add(term)
                break
    return matched


def _is_on_topic(item: _Item, terms: set[str]) -> bool:
    """Whether the result may be offered as evidence for this query at all.

    Applied before independent publishers are counted. A publisher that
    answered with something off topic has not corroborated anything, so it must
    not satisfy the loop's "a second publisher replied" condition either.
    """
    if not terms:
        return True
    return len(_matched_terms(item, terms)) >= min(_MIN_TOPIC_TERMS, len(terms))


def _query_variants(query: str) -> list[str]:
    """The query itself, then progressively looser keyword forms."""
    variants = [query.strip()]
    for limit in (4, 2):
        candidate = _keywords(query, limit)
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


@dataclass(frozen=True)
class _Item:
    title: str
    url: str
    snippet: str
    publisher: str
    published_at: str | None
    modified_at: str | None = None


class FreeNewsSearchProvider:
    """A ``SearchProvider`` backed by endpoints that cost nothing to call.

    ``fetcher`` is the project's document fetcher, reused so that robots
    handling, timeouts and the on-disk document store stay in one place.
    """

    name = "free-news"

    #: These endpoints need no key and charge nothing, so reserving budget
    #: against them would both overstate the day's spend and make a failed
    #: search look like one with an uncertain bill.
    cost_per_search_usd = 0.0

    def __init__(self, fetcher, *, per_publisher: int = 4, timeout: float = 20.0) -> None:
        self.fetcher = fetcher
        self.per_publisher = per_publisher
        self.timeout = timeout

    def search(self, query: str, *, language: str = "de", count: int = 8) -> SearchResponse:
        terms = _topic_terms(query)
        items: list[_Item] = []
        off_topic = 0
        errors: list[str] = []
        for variant in _query_variants(query):
            for source in (self._tagesschau, self._wikipedia):
                try:
                    returned = source(variant, language=language)
                except Exception as exc:  # noqa: BLE001 - one channel failing is not fatal
                    errors.append(
                        f"{source.__name__.lstrip('_')}: {type(exc).__name__}: {exc}"
                    )
                    continue
                for item in returned:
                    if _is_on_topic(item, terms):
                        items.append(item)
                    else:
                        off_topic += 1
            # Keep loosening while only one publisher answered *on topic*. A
            # single publisher is exactly the case the independence check has
            # to reject, so stopping there would manufacture that verdict --
            # and stopping on an off-topic reply would manufacture the
            # opposite one, out of a document about a different subject.
            if len({item.publisher for item in items}) >= 2:
                break
        if not items:
            detail = []
            if off_topic:
                detail.append(
                    f"{off_topic} result(s) were discarded as off topic for "
                    f"{sorted(terms)}"
                )
            detail.extend(errors)
            raise SearchProviderError(
                "No free search channel returned a usable result"
                + (f" ({'; '.join(detail)})" if detail else "")
            )
        hits = [
            SearchHit(
                url=item.url,
                title=item.title,
                snippet=item.snippet,
                publisher=item.publisher,
                published_at=item.published_at,
                modified_at=item.modified_at,
            )
            for item in _deduplicate(items)[: max(1, count)]
        ]
        return SearchResponse(
            provider=self.name, query=query, hits=tuple(hits), cost_usd=0.0
        )

    # -- channels ---------------------------------------------------------

    def _tagesschau(self, query: str, *, language: str) -> list[_Item]:
        url = TAGESSCHAU_SEARCH + "?" + urllib.parse.urlencode(
            {"searchText": query[:300], "pageSize": self.per_publisher * 3}
        )
        payload = self._json(url)
        items: list[_Item] = []
        for row in payload.get("searchResults", []):
            share = (row.get("shareURL") or "").strip()
            if not share or row.get("type") in _UNREADABLE:
                continue
            if not share.startswith("https://www.tagesschau.de/"):
                continue
            items.append(
                _Item(
                    title=str(row.get("title") or "").strip(),
                    url=share,
                    snippet=str(row.get("firstSentence") or "").strip(),
                    publisher="tagesschau.de",
                    published_at=_iso_date(row.get("date")),
                )
            )
            if len(items) >= self.per_publisher:
                break
        return items

    def _wikipedia(self, query: str, *, language: str) -> list[_Item]:
        host = "de" if language.startswith("de") else "en"
        url = f"https://{host}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query[:300],
                "srlimit": self.per_publisher,
                "format": "json",
            }
        )
        payload = self._json(url)
        items: list[_Item] = []
        for row in payload.get("query", {}).get("search", []):
            title = str(row.get("title") or "")
            if not title:
                continue
            items.append(
                _Item(
                    title=title,
                    url=(
                        f"https://{host}.wikipedia.org/wiki/"
                        + urllib.parse.quote(title.replace(" ", "_"))
                    ),
                    snippet=_strip_tags(str(row.get("snippet") or "")),
                    publisher=f"{host}.wikipedia.org",
                    # MediaWiki reports the last *revision* time. An article
                    # about a 19th-century museum edited yesterday is not
                    # yesterday's reporting, and a wiki page has no
                    # publication date in the sense this field carries
                    # everywhere else, so it stays unknown.
                    published_at=None,
                    modified_at=_iso_date(row.get("timestamp")),
                )
            )
        return items

    # -- transport --------------------------------------------------------

    def _json(self, url: str) -> dict:
        document = self.fetcher.fetch(url)
        raw = _read(document)
        return json.loads(raw)


def _read(document) -> str:
    from pathlib import Path

    return Path(document.body_path).read_text(encoding="utf-8", errors="replace")


def _strip_tags(value: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", value).strip()


def _iso_date(value: object) -> str | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else None


def _deduplicate(items: list[_Item]) -> list[_Item]:
    """One hit per URL, publishers interleaved.

    Interleaving matters: taking the first ``limit`` hits from a flat list
    would hand the research step eight results from one publisher and call the
    claim corroborated.
    """
    by_publisher: dict[str, list[_Item]] = {}
    seen: set[str] = set()
    for item in items:
        if not item.url or item.url in seen:
            continue
        seen.add(item.url)
        by_publisher.setdefault(item.publisher, []).append(item)
    ordered: list[_Item] = []
    while any(by_publisher.values()):
        for bucket in by_publisher.values():
            if bucket:
                ordered.append(bucket.pop(0))
    return ordered
