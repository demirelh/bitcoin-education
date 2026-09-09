"""Provider-neutral web search contracts for newsroom research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import requests


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    publisher: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    provider: str
    query: str
    hits: tuple[SearchHit, ...]
    request_id: str | None = None
    cost_usd: float = 0.0


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, language: str, count: int) -> SearchResponse: ...


class SearchProviderError(RuntimeError):
    pass


class SearchRateLimited(SearchProviderError):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class FixtureSearchProvider:
    """Deterministic provider used by tests and offline editorial fixtures."""

    name = "fixture"

    def __init__(self, responses: dict[tuple[str, str], SearchResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, int]] = []

    def search(self, query: str, *, language: str, count: int) -> SearchResponse:
        self.calls.append((query, language, count))
        response = self.responses[(query, language)]
        return SearchResponse(
            provider=response.provider,
            query=response.query,
            hits=response.hits[:count],
            request_id=response.request_id,
            cost_usd=response.cost_usd,
        )


class BraveSearchProvider:
    """Minimal Brave Web Search adapter; account activation remains operator-owned."""

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Brave Search API key is required")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.trust_env = False

    def search(self, query: str, *, language: str, count: int) -> SearchResponse:
        if language not in {"de", "tr", "en"}:
            raise ValueError(f"Unsupported search language: {language}")
        response = self.session.get(
            self.endpoint,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
            },
            params={
                "q": query,
                "search_lang": language,
                "count": min(max(count, 1), 20),
                "safesearch": "moderate",
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code == 429:
            raise SearchRateLimited(
                "Brave Search rate limited the request",
                _retry_after_seconds(response.headers.get("Retry-After")),
            )
        if response.status_code >= 500:
            raise SearchProviderError(
                f"Brave Search returned transient HTTP {response.status_code}"
            )
        response.raise_for_status()
        payload = response.json()
        hits = tuple(
            SearchHit(
                title=str(item.get("title") or ""),
                url=str(item["url"]),
                snippet=str(item.get("description") or ""),
                publisher=_publisher(item),
                published_at=item.get("page_age"),
            )
            for item in (payload.get("web") or {}).get("results", [])
            if item.get("url")
        )
        return SearchResponse(
            provider=self.name,
            query=query,
            hits=hits,
            request_id=response.headers.get("X-Request-Id"),
        )


def _publisher(item: dict) -> str | None:
    profile = item.get("profile") or {}
    value = profile.get("long_name") or profile.get("name")
    return str(value) if value else None


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
