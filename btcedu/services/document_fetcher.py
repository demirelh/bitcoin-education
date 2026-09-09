"""SSRF-resistant, size-bounded fetching for untrusted evidence pages."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import urllib3

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/json",
    }
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class DocumentFetchError(RuntimeError):
    pass


class DocumentHTTPError(DocumentFetchError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"Document source returned HTTP {status_code}")
        self.status_code = status_code


class UnsafeDocumentURL(DocumentFetchError):
    pass


class UnsupportedDocument(DocumentFetchError):
    pass


class DocumentTooLarge(DocumentFetchError):
    pass


class DocumentRateLimited(DocumentFetchError):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class RawDocumentResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    content_type: str
    body: bytes
    text: str
    title: str | None
    content_hash: str
    retrieved_at: datetime
    from_cache: bool = False
    body_path: str | None = None


Resolver = Callable[[str, int], list[str]]
Transport = Callable[[str, str, float, float, int], RawDocumentResponse]


class DocumentSnapshotStore:
    """Private on-disk cache retaining the exact fetched bytes and metadata."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load_fresh(
        self,
        url: str,
        *,
        now: datetime,
        max_age: timedelta,
    ) -> FetchedDocument | None:
        directory = self.root / _url_digest(url)
        latest = directory / "latest.json"
        if not latest.is_file():
            return None
        try:
            metadata = json.loads(latest.read_text(encoding="utf-8"))
            retrieved_at = datetime.fromisoformat(metadata["retrieved_at"])
            if retrieved_at.tzinfo is None:
                retrieved_at = retrieved_at.replace(tzinfo=UTC)
            if now - retrieved_at > max_age:
                return None
            body_path = directory / metadata["body_file"]
            body = body_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != metadata["content_hash"]:
                return None
            return FetchedDocument(
                requested_url=metadata["requested_url"],
                final_url=metadata["final_url"],
                content_type=metadata["content_type"],
                body=body,
                text=metadata["text"],
                title=metadata.get("title"),
                content_hash=metadata["content_hash"],
                retrieved_at=retrieved_at,
                from_cache=True,
                body_path=str(body_path),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def store(self, document: FetchedDocument) -> FetchedDocument:
        directory = self.root / _url_digest(document.requested_url)
        directory.mkdir(parents=True, exist_ok=True)
        body_name = f"{document.content_hash}.bin"
        body_path = directory / body_name
        if not body_path.exists():
            _atomic_write(body_path, document.body)
        metadata = {
            "requested_url": document.requested_url,
            "final_url": document.final_url,
            "content_type": document.content_type,
            "content_hash": document.content_hash,
            "retrieved_at": document.retrieved_at.isoformat(),
            "title": document.title,
            "text": document.text,
            "body_file": body_name,
        }
        _atomic_write(
            directory / "latest.json",
            json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        return FetchedDocument(
            **{
                **document.__dict__,
                "body_path": str(body_path),
            }
        )


class DocumentFetcher:
    """Fetch public HTML/text without proxies, scripts, unbounded bodies or private IPs."""

    def __init__(
        self,
        *,
        store: DocumentSnapshotStore,
        resolver: Resolver | None = None,
        transport: Transport | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 20.0,
        max_redirects: int = 3,
        max_bytes: int = 2 * 1024 * 1024,
        cache_ttl: timedelta = timedelta(hours=6),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.resolver = resolver or _resolve
        self.transport = transport or _pinned_request
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.cache_ttl = cache_ttl
        self.now = now or (lambda: datetime.now(UTC))

    @classmethod
    def from_settings(cls, settings) -> DocumentFetcher:
        return cls(
            store=DocumentSnapshotStore(Path(settings.newsroom_data_dir) / "documents"),
            connect_timeout_seconds=settings.newsroom_fetch_connect_timeout_seconds,
            read_timeout_seconds=settings.newsroom_fetch_read_timeout_seconds,
            max_redirects=settings.newsroom_fetch_max_redirects,
            max_bytes=settings.newsroom_fetch_max_bytes,
            cache_ttl=timedelta(seconds=settings.newsroom_fetch_cache_ttl_seconds),
        )

    def fetch(self, url: str) -> FetchedDocument:
        requested_url = _canonical_url(url)
        now = self.now()
        cached = self.store.load_fresh(requested_url, now=now, max_age=self.cache_ttl)
        if cached is not None:
            return cached

        current_url = requested_url
        for redirect_count in range(self.max_redirects + 1):
            parsed = _validated_url(current_url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = self.resolver(parsed.hostname or "", port)
            address = _validated_public_address(addresses)
            response = self.transport(
                current_url,
                address,
                self.connect_timeout_seconds,
                self.read_timeout_seconds,
                self.max_bytes,
            )

            if response.status_code in REDIRECT_STATUSES:
                if redirect_count >= self.max_redirects:
                    raise DocumentFetchError("Too many redirects")
                location = _header(response.headers, "location")
                if not location:
                    raise DocumentFetchError("Redirect response has no Location header")
                current_url = _canonical_url(urljoin(current_url, location))
                continue
            if response.status_code == 429:
                raise DocumentRateLimited(
                    "Document source rate limited the request",
                    _retry_after_seconds(_header(response.headers, "retry-after"), now),
                )
            if response.status_code >= 500:
                raise DocumentHTTPError(response.status_code)
            if response.status_code >= 400:
                raise DocumentHTTPError(response.status_code)
            if len(response.body) > self.max_bytes:
                raise DocumentTooLarge(f"Document exceeds {self.max_bytes} bytes")

            content_type = (_header(response.headers, "content-type") or "").split(";", 1)[0]
            content_type = content_type.strip().lower()
            if content_type not in ALLOWED_CONTENT_TYPES:
                raise UnsupportedDocument(
                    f"Unsupported document content type: {content_type or 'missing'}"
                )
            text, title = _extract_text(response.body, content_type)
            document = FetchedDocument(
                requested_url=requested_url,
                final_url=current_url,
                content_type=content_type,
                body=response.body,
                text=text,
                title=title,
                content_hash=hashlib.sha256(response.body).hexdigest(),
                retrieved_at=now,
            )
            return self.store.store(document)

        raise DocumentFetchError("Redirect handling terminated unexpectedly")


def _resolve(host: str, port: int) -> list[str]:
    return sorted(
        {
            result[4][0]
            for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    )


def _validated_public_address(addresses: list[str]) -> str:
    if not addresses:
        raise UnsafeDocumentURL("Host did not resolve")
    parsed = [ipaddress.ip_address(address) for address in addresses]
    if any(not address.is_global for address in parsed):
        raise UnsafeDocumentURL("Host resolves to a non-public address")
    return str(parsed[0])


def _validated_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeDocumentURL("Only http and https URLs are supported")
    if not parsed.hostname:
        raise UnsafeDocumentURL("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeDocumentURL("Credentials in URLs are not allowed")
    return parsed


def _canonical_url(url: str) -> str:
    parsed = _validated_url(url.strip())
    hostname = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    if port and port != (443 if parsed.scheme == "https" else 80):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), hostname, path, parsed.query, ""))


def _pinned_request(
    url: str,
    address: str,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    max_bytes: int,
) -> RawDocumentResponse:
    parsed = _validated_url(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    timeout = urllib3.Timeout(connect=connect_timeout_seconds, read=read_timeout_seconds)
    headers = {
        "Accept": "text/html,application/xhtml+xml,text/plain,application/json",
        "Accept-Encoding": "gzip, deflate",
        "Host": parsed.netloc,
        "User-Agent": "ALMANYA24-Newsroom/1.0 (+editorial research)",
    }
    pool_class = (
        urllib3.HTTPSConnectionPool
        if parsed.scheme == "https"
        else urllib3.HTTPConnectionPool
    )
    kwargs = {"host": address, "port": port, "timeout": timeout, "maxsize": 1}
    if parsed.scheme == "https":
        kwargs.update(
            {
                "server_hostname": parsed.hostname,
                "assert_hostname": parsed.hostname,
                "cert_reqs": "CERT_REQUIRED",
            }
        )
    pool = pool_class(**kwargs)
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    response = None
    try:
        response = pool.urlopen(
            "GET",
            target,
            headers=headers,
            redirect=False,
            preload_content=False,
            decode_content=True,
        )
        body = response.read(max_bytes + 1, decode_content=True)
        return RawDocumentResponse(
            status_code=response.status,
            headers=dict(response.headers),
            body=body,
        )
    finally:
        if response is not None:
            response.release_conn()
        pool.close()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.parts.append(value)
        if self._in_title:
            self.title_parts.append(value)


def _extract_text(body: bytes, content_type: str) -> tuple[str, str | None]:
    text = body.decode("utf-8", errors="replace")
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _TextExtractor()
        parser.feed(text)
        return " ".join(parser.parts), " ".join(parser.title_parts) or None
    if content_type == "application/json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UnsupportedDocument("Invalid JSON document") from exc
        return json.dumps(payload, ensure_ascii=False, sort_keys=True), None
    return " ".join(text.split()), None


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
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
        return max(0.0, (retry_at - now).total_seconds())


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _url_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    temporary.replace(path)
