"""The key-less search adapter, and the query shapes these endpoints accept.

The live failure this file pins down: both endpoints match every term of a
query, so handing them a full headline retrieves nothing at all. Seven of
eight stories in the first real run died on that, with no error anywhere --
the endpoints answered perfectly well, with zero results.
"""

from __future__ import annotations

import json

from btcedu.services.free_search import (
    FreeNewsSearchProvider,
    _keywords,
    _query_variants,
)


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
                        "title": "Drohnenanschlag auf Flughafen Leipzig-Halle",
                        "firstSentence": "Die Spur führt zum GRU.",
                        "date": "2026-09-12",
                    }
                ]
            }
        return {
            "query": {
                "search": [
                    {
                        "title": "GRU (Nachrichtendienst)",
                        "snippet": "Der GRU wird für Anschläge auf Flughafen-"
                        "Infrastruktur verantwortlich gemacht.",
                        "timestamp": "2026-09-01",
                    }
                ]
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
                        "title": "Lange deutsche Schlagzeile zum Thema",
                        "firstSentence": "Eine Schlagzeile über dieses Thema.",
                        "date": "2026-09-12",
                    }
                ]
            }
        return {"query": {"search": []}}

    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, responses))
    result = provider.search("Eine lange deutsche Schlagzeile über ein Thema", count=8)

    assert len(result.hits) == 1


def test_an_answered_search_without_hits_is_an_empty_response(tmp_path):
    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, _empty))
    result = provider.search("Eine Schlagzeile ohne jeden Treffer", count=8)
    assert result.hits == ()


def test_the_adapter_costs_nothing(tmp_path):
    def responses(url, term):
        if "tagesschau" in url:
            return {
                "searchResults": [
                    {
                        "shareURL": "https://www.tagesschau.de/a.html",
                        "title": "Thema des Tages",
                        "firstSentence": "Zum Thema gibt es Neues.",
                        "date": "2026-09-12",
                    }
                ]
            }
        return {"query": {"search": [{"title": "Thema", "snippet": "Zum Thema."}]}}

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
# Both are fixed; the tests below are the regression cover for the fixes.
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

#: `news_research_queries` row 672, verbatim. Same story, different claim --
#: and a second way to share one incidental word with an unrelated article.
_INSOLVENZ_WIKIPEDIA_HITS = {
    "query": {
        "search": [
            {
                "title": "Belle Époque",
                "snippet": (
                    "Die Einkaufspassage Galleria Umberto I in der Altstadt Neapels "
                    "wurde in den Jahren 1887 bis 1890 nach dem Vorbild der Galleria "
                    "Vittorio Emanuele II in"
                ),
                "timestamp": "2026-09-24T00:00:00Z",
            },
            {
                "title": "Migros Ticaret",
                "snippet": (
                    "Karl Ketterer wurde erster Geschäftsführer des Unternehmens, und "
                    "acht türkische Beamte kamen in die Schweiz zur Ausbildung. Das "
                    "erste Warenhaus wurde"
                ),
                "timestamp": "2026-08-04T00:00:00Z",
            },
        ]
    }
}

_GALLERIA_CLAIM = "Galleria-Filialen Vorerst bleiben die Filialen geöffnet."
_INSOLVENZ_CLAIM = (
    "Die Warenhaus-Kette Galleria ist wieder insolvent, zum 4. Mal innerhalb von 6 Jahren."
)


def _only_wikipedia(payload):
    def responses(url, term):
        if "tagesschau" in url:
            return {"searchResults": []}
        return payload

    return responses


def test_an_encyclopedia_hit_sharing_one_incidental_word_is_not_evidence(tmp_path):
    """The recorded Galleria failure: 'Galleria' alone is not topical overlap."""
    provider = FreeNewsSearchProvider(
        _Fetcher(tmp_path, _only_wikipedia(_GALLERIA_WIKIPEDIA_HITS))
    )
    assert provider.search(_GALLERIA_CLAIM, language="de", count=8).hits == ()


def test_a_shared_calendar_word_does_not_make_a_result_relevant(tmp_path):
    """'Galleria' plus 'Jahren' is still one topic term, not two."""
    provider = FreeNewsSearchProvider(
        _Fetcher(tmp_path, _only_wikipedia(_INSOLVENZ_WIKIPEDIA_HITS))
    )
    assert provider.search(_INSOLVENZ_CLAIM, language="de", count=8).hits == ()


def test_relevance_is_judged_before_a_second_publisher_is_counted(tmp_path):
    """An off-topic second publisher must not end the loosening loop."""

    def responses(url, term):
        if "tagesschau" in url:
            # Answers only the loosest variant, as the live endpoint did.
            if len(term.split()) > 2:
                return {"searchResults": []}
            return {
                "searchResults": [
                    {
                        "shareURL": "https://www.tagesschau.de/galleria-insolvenz.html",
                        "title": "Galleria-Filialen bleiben vorerst geöffnet",
                        "firstSentence": "Die Filialen der Warenhaus-Kette Galleria "
                        "bleiben vorerst geöffnet.",
                        "date": "2026-10-02",
                    }
                ]
            }
        return _GALLERIA_WIKIPEDIA_HITS

    fetcher = _Fetcher(tmp_path, responses)
    provider = FreeNewsSearchProvider(fetcher)
    result = provider.search(_GALLERIA_CLAIM, language="de", count=8)

    assert [hit.publisher for hit in result.hits] == ["tagesschau.de"]
    assert len(fetcher.queries) > 2, (
        "the off-topic Wikipedia reply ended the search before the news "
        "archive was asked a query it could answer"
    )


def test_an_on_topic_encyclopedia_hit_is_still_accepted(tmp_path):
    """The fix must reject off-topic results, not Wikipedia as a publisher."""

    def responses(url, term):
        if "tagesschau" in url:
            return {"searchResults": []}
        return {
            "query": {
                "search": [
                    {
                        "title": "Galleria Karstadt Kaufhof",
                        "snippet": "Die Warenhaus-Kette Galleria meldete erneut "
                        "Insolvenz an; einzelne Filialen bleiben geöffnet.",
                        "timestamp": "2026-10-03T08:12:00Z",
                    }
                ]
            }
        }

    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, responses))
    result = provider.search(_GALLERIA_CLAIM, language="de", count=8)

    assert [hit.title for hit in result.hits] == ["Galleria Karstadt Kaufhof"]


def test_a_wikipedia_revision_date_is_never_a_publication_date(tmp_path):
    def responses(url, term):
        if "tagesschau" in url:
            return {"searchResults": []}
        return {
            "query": {
                "search": [
                    {
                        "title": "Galleria Karstadt Kaufhof",
                        "snippet": "Die Warenhaus-Kette Galleria meldete erneut "
                        "Insolvenz an; einzelne Filialen bleiben geöffnet.",
                        "timestamp": "2026-10-03T08:12:00Z",
                    }
                ]
            }
        }

    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, responses))
    hit = provider.search(_GALLERIA_CLAIM, language="de", count=8).hits[0]

    assert hit.published_at is None, "a revision time is not a publication date"
    assert hit.modified_at == "2026-10-03", "the revision time is kept, just not as news"


def test_a_news_article_keeps_its_real_publication_date(tmp_path):
    def responses(url, term):
        if "tagesschau" in url:
            return {
                "searchResults": [
                    {
                        "shareURL": "https://www.tagesschau.de/galleria-insolvenz.html",
                        "title": "Galleria-Filialen bleiben vorerst geöffnet",
                        "firstSentence": "Die Filialen der Warenhaus-Kette Galleria "
                        "bleiben vorerst geöffnet.",
                        "date": "2026-10-02",
                    }
                ]
            }
        return {"query": {"search": []}}

    provider = FreeNewsSearchProvider(_Fetcher(tmp_path, responses))
    hit = provider.search(_GALLERIA_CLAIM, language="de", count=8).hits[0]

    assert hit.published_at == "2026-10-02"
    assert hit.modified_at is None
