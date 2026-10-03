"""The key-less search adapter, and the query shapes these endpoints accept.

The live failure this file pins down: both endpoints match every term of a
query, so handing them a full headline retrieves nothing at all. Seven of
eight stories in the first real run died on that, with no error anywhere --
the endpoints answered perfectly well, with zero results.
"""

from __future__ import annotations

import json

import pytest

from btcedu.services.free_search import (
    FreeNewsSearchProvider,
    _keywords,
    _query_variants,
)
from btcedu.services.search_service import SearchProviderError


class _Doc:
    def __init__(self, path):
        self.body_path = path


class _Fetcher:
    """Answers from a canned map, and records what was asked."""

    def __init__(self, tmp_path, responses):
        self.tmp_path = tmp_path
        self.responses = responses
        self.queries: list[str] = []
        self._n = 0

    def fetch(self, url):
        import urllib.parse

        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        term = (params.get("searchText") or params.get("srsearch") or [""])[0]
        self.queries.append(term)
        payload = self.responses(url, term)
        self._n += 1
        path = self.tmp_path / f"doc{self._n}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return _Doc(str(path))


def _empty(url, term):
    return {"searchResults": [], "query": {"search": []}}


def test_a_full_headline_is_reduced_to_its_carrying_terms():
    headline = "Gescheiterter Drohnenanschlag auf Flughafen Leipzig-Halle: Spur führt zum GRU"
    assert _keywords(headline, 4) == "Gescheiterter Drohnenanschlag Flughafen Leipzig-Halle"


def test_function_words_never_survive():
    assert _keywords("Der Gipfel in Indien und die Folgen für Europa", 6) == (
        "Gipfel Indien Folgen Europa"
    )


def test_variants_start_with_the_untouched_query():
    variants = _query_variants("BRICS-Gipfel in Indien befasst sich mit Nahostkonflikt")
    assert variants[0] == "BRICS-Gipfel in Indien befasst sich mit Nahostkonflikt"
    assert len(variants[1].split()) > len(variants[2].split())


def test_a_lowercase_query_still_yields_terms():
    """German capitalises nouns, but a query need not be German prose."""
    assert _keywords("bundesweiter warntag sirenen", 3) == "bundesweiter warntag sirenen"


def test_the_search_loosens_until_two_publishers_answer(tmp_path):
    """One publisher is the verdict the independence check must be free to
    reach on the evidence -- not one the adapter imposes by stopping early."""

    def responses(url, term):
        narrow = len(term.split()) > 4
        if "tagesschau" in url:
            if narrow:
                return {"searchResults": []}
            return {
                "searchResults": [
                    {
                        "shareURL": "https://www.tagesschau.de/a.html",
                        "title": "A",
                        "firstSentence": "s",
                        "date": "2026-09-12",
                    }
                ]
            }
        return {
            "query": {
                "search": [{"title": "Thema", "snippet": "s", "timestamp": "2026-09-01"}]
            }
        }

    fetcher = _Fetcher(tmp_path, responses)
    provider = FreeNewsSearchProvider(fetcher)
    result = provider.search(
        "Gescheiterter Drohnenanschlag auf Flughafen Leipzig-Halle Spur GRU",
        language="de",
        count=8,
    )

    assert {hit.publisher for hit in result.hits} == {"tagesschau.de", "de.wikipedia.org"}
    assert len(fetcher.queries) > 2, "the adapter gave up on the first, narrow query"


def test_a_repeated_url_is_not_counted_twice_across_variants(tmp_path):
    def responses(url, term):
        if "tagesschau" in url:
            return {
                "searchResults": [
                    {
                        "shareURL": "https://www.tagesschau.de/same.html",
                        "title": "A",
                        "firstSentence": "s",
                        "date": "2026-09-12",
                    }
                ]
            }
        return {"query": {"search": []}}

    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, responses))
    result = provider.search("Eine lange deutsche Schlagzeile über ein Thema", count=8)

    assert len(result.hits) == 1


def test_an_empty_result_after_every_variant_is_an_error(tmp_path):
    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, _empty))
    with pytest.raises(SearchProviderError):
        provider.search("Eine Schlagzeile ohne jeden Treffer", count=8)


def test_the_adapter_costs_nothing(tmp_path):
    def responses(url, term):
        if "tagesschau" in url:
            return {
                "searchResults": [
                    {
                        "shareURL": "https://www.tagesschau.de/a.html",
                        "title": "A",
                        "firstSentence": "s",
                        "date": "2026-09-12",
                    }
                ]
            }
        return {"query": {"search": [{"title": "T", "snippet": "s"}]}}

    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, responses))
    assert provider.search("Thema", count=4).cost_usd == 0.0


# ---------------------------------------------------------------------------
# Recorded defect: the Galleria query answered with unrelated encyclopedia
# articles.
#
# Live run 2026-10-03, research_run_id 100, news_research_queries rows 674/675.
# The claim under research was `filialen_bleiben_geoeffnet` from the Galleria
# insolvency story. Both the support and the counter query came back with two
# de.wikipedia.org hits about a Turin picture gallery ("Hans Memling") and a
# Trieste museum ("Triest"). The payloads below are those recorded responses,
# so the reproduction needs no network and no provider call.
#
# The evaluator did the right thing with them and recorded `inconclusive`, so
# the evidence gate is not what is broken here. Two separate defects in this
# adapter are:
#
#   1. Wikipedia's full-text index matches the incidental Italian word
#      "Galleria", and the loosening loop in `search()` stops as soon as a
#      second publisher has answered *anything*. An off-topic encyclopedia hit
#      therefore ends the search and is presented as an independent second
#      source.
#   2. `_wikipedia()` maps MediaWiki's `timestamp` -- the last *revision* time
#      -- onto `published_at`, the same field a news article fills with its
#      publication date. A 19th-century museum article last edited ten days
#      ago is handed downstream looking like ten-day-old reporting.
#
# The two xfail tests below state the intended behaviour. When either is
# fixed, its strict xfail turns into a failure and must be removed.
# ---------------------------------------------------------------------------

#: `news_research_queries` row 674, verbatim.
_GALLERIA_WIKIPEDIA_HITS = {
    "query": {
        "search": [
            {
                "title": "Triest",
                "snippet": (
                    "Museum Revoltella – Galerie für Moderne Kunst (Civico Museo "
                    "Revoltella – Galleria d’Arte Moderna) ist eines der größten "
                    "und bedeutendsten Museen der Stadt"
                ),
                "timestamp": "2026-09-23T11:04:00Z",
            },
            {
                "title": "Hans Memling",
                "snippet": (
                    "den Flügeln der Stifter Bürgermeister Moreel mit seiner Familie "
                    "In der Galleria Sabauda in Turin befindet sich eine Tafel, die in "
                    "verschiedenen kleinen"
                ),
                "timestamp": "2026-10-03T08:12:00Z",
            },
        ]
    }
}

_GALLERIA_CLAIM = "Galleria-Filialen Vorerst bleiben die Filialen geöffnet."


def _galleria_responses(url, term):
    """tagesschau has nothing on it; Wikipedia answers every variant."""
    if "tagesschau" in url:
        return {"searchResults": []}
    return _GALLERIA_WIKIPEDIA_HITS


def test_the_galleria_query_really_does_return_unrelated_wikipedia_articles(tmp_path):
    """Pins the reproduction itself, so it cannot rot away unnoticed."""
    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, _galleria_responses))
    result = provider.search(_GALLERIA_CLAIM, language="de", count=8)

    titles = {hit.title for hit in result.hits}
    assert titles == {"Triest", "Hans Memling"}
    assert all(hit.publisher == "de.wikipedia.org" for hit in result.hits)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect: a Wikipedia hit that matches only on the incidental word "
        "'Galleria' is handed to the research step as evidence for a German "
        "department-store insolvency."
    ),
)
def test_an_off_topic_encyclopedia_hit_is_not_offered_as_evidence(tmp_path):
    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, _galleria_responses))
    result = provider.search(_GALLERIA_CLAIM, language="de", count=8)

    assert result.hits == ()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Defect: MediaWiki's last-revision timestamp is written into "
        "published_at, so an encyclopedia article looks like recent reporting."
    ),
)
def test_a_wikipedia_revision_date_is_not_passed_off_as_a_publication_date(tmp_path):
    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, _galleria_responses))
    result = provider.search(_GALLERIA_CLAIM, language="de", count=8)

    by_title = {hit.title: hit for hit in result.hits}
    assert by_title["Triest"].published_at is None
