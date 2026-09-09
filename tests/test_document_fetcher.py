import ipaddress
from datetime import UTC, datetime, timedelta

import pytest

from btcedu.services.document_fetcher import (
    DocumentFetcher,
    DocumentFetchError,
    DocumentRateLimited,
    DocumentSnapshotStore,
    DocumentTooLarge,
    RawDocumentResponse,
    UnsafeDocumentURL,
    UnsupportedDocument,
)

PUBLIC_IP = "93.184.216.34"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, address, connect_timeout, read_timeout, max_bytes):
        self.calls.append((url, address, connect_timeout, read_timeout, max_bytes))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response(
    body: bytes = b"<html><title>Example</title><p>Public evidence.</p></html>",
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    **headers,
):
    return RawDocumentResponse(
        status_code=status,
        headers={"Content-Type": content_type, **headers},
        body=body,
    )


def _fetcher(tmp_path, transport, *, resolver=None, now=None, max_bytes=1024):
    return DocumentFetcher(
        store=DocumentSnapshotStore(tmp_path / "newsroom" / "documents"),
        transport=transport,
        resolver=resolver or (lambda host, port: [PUBLIC_IP]),
        max_bytes=max_bytes,
        now=now,
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user:password@example.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_fetcher_rejects_non_public_or_credentialed_urls(tmp_path, url):
    transport = FakeTransport([])

    def resolver(host, port):
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return [PUBLIC_IP]

    fetcher = _fetcher(tmp_path, transport, resolver=resolver)

    with pytest.raises(UnsafeDocumentURL):
        fetcher.fetch(url)

    assert transport.calls == []


def test_fetcher_revalidates_every_redirect_and_blocks_dns_change(tmp_path):
    transport = FakeTransport(
        [
            _response(status=302, Location="/next"),
        ]
    )
    resolutions = iter([[PUBLIC_IP], ["10.0.0.8"]])
    fetcher = _fetcher(tmp_path, transport, resolver=lambda host, port: next(resolutions))

    with pytest.raises(UnsafeDocumentURL, match="non-public"):
        fetcher.fetch("https://example.com/start")

    assert len(transport.calls) == 1


def test_fetcher_strips_scripts_and_caches_exact_bytes(tmp_path):
    body = (
        b"<html><title>Evidence title</title><script>ignore me</script>"
        b"<p>Berlin meldet 100 Wohnungen.</p></html>"
    )
    transport = FakeTransport([_response(body)])
    current = datetime(2026, 9, 9, tzinfo=UTC)
    fetcher = _fetcher(tmp_path, transport, now=lambda: current)

    first = fetcher.fetch("https://EXAMPLE.com/report#fragment")
    second = fetcher.fetch("https://example.com/report")

    assert first.from_cache is False
    assert second.from_cache is True
    assert second.body == body
    assert second.title == "Evidence title"
    assert "ignore me" not in second.text
    assert "Berlin meldet 100 Wohnungen." in second.text
    assert len(transport.calls) == 1


def test_stale_cache_is_refetched(tmp_path):
    responses = FakeTransport([_response(b"first"), _response(b"second")])
    current = datetime(2026, 9, 9, tzinfo=UTC)
    fetcher = _fetcher(tmp_path, responses, now=lambda: current)
    fetcher.fetch("https://example.com/report")
    current += timedelta(hours=7)

    refreshed = fetcher.fetch("https://example.com/report")

    assert refreshed.body == b"second"
    assert len(responses.calls) == 2


def test_fetcher_rejects_oversize_or_wrong_mime(tmp_path):
    oversized = _fetcher(
        tmp_path,
        FakeTransport([_response(b"x" * 11)]),
        max_bytes=10,
    )
    with pytest.raises(DocumentTooLarge):
        oversized.fetch("https://example.com/large")

    unsupported = _fetcher(
        tmp_path,
        FakeTransport([_response(b"%PDF", content_type="application/pdf")]),
    )
    with pytest.raises(UnsupportedDocument):
        unsupported.fetch("https://example.com/report.pdf")


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_fetcher_surfaces_http_failures(tmp_path, status):
    fetcher = _fetcher(tmp_path, FakeTransport([_response(status=status)]))

    with pytest.raises(DocumentFetchError, match=str(status)):
        fetcher.fetch(f"https://example.com/{status}")


def test_fetcher_parses_retry_after(tmp_path):
    fetcher = _fetcher(
        tmp_path,
        FakeTransport([_response(status=429, **{"Retry-After": "12"})]),
    )

    with pytest.raises(DocumentRateLimited) as error:
        fetcher.fetch("https://example.com/rate")

    assert error.value.retry_after_seconds == 12


def test_fetcher_rejects_redirect_loops(tmp_path):
    transport = FakeTransport(
        [_response(status=302, Location="/again") for _ in range(4)]
    )
    fetcher = _fetcher(tmp_path, transport)

    with pytest.raises(DocumentFetchError, match="Too many redirects"):
        fetcher.fetch("https://example.com/start")
