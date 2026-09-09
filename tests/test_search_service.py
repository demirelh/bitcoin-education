import pytest

from btcedu.services.search_service import BraveSearchProvider, SearchRateLimited


class FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {"X-Request-Id": "request-1"}

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "web": {
                "results": [
                    {
                        "title": "Official result",
                        "url": "https://example.com/report",
                        "description": "A discovery snippet.",
                        "page_age": "2026-09-09T00:00:00Z",
                        "profile": {"long_name": "Example Office"},
                    }
                ]
            }
        }


class FakeSession:
    def __init__(self, response=None):
        self.trust_env = True
        self.calls = []
        self.response = response or FakeResponse()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_brave_adapter_maps_results_without_exposing_key():
    session = FakeSession()
    provider = BraveSearchProvider("secret-key", session=session)

    result = provider.search("Berlin housing", language="de", count=5)

    assert session.trust_env is False
    assert result.provider == "brave"
    assert result.request_id == "request-1"
    assert result.hits[0].publisher == "Example Office"
    assert result.hits[0].snippet == "A discovery snippet."
    _, request = session.calls[0]
    assert request["params"]["search_lang"] == "de"
    assert request["params"]["count"] == 5
    assert request["headers"]["X-Subscription-Token"] == "secret-key"


def test_brave_adapter_surfaces_retry_after_without_retrying():
    session = FakeSession(FakeResponse(429, {"Retry-After": "9"}))
    provider = BraveSearchProvider("secret-key", session=session)

    with pytest.raises(SearchRateLimited) as error:
        provider.search("Berlin housing", language="de", count=5)

    assert error.value.retry_after_seconds == 9
    assert len(session.calls) == 1
