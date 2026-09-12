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
